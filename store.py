"""SQLite-backed fact store with entity resolution and trust scoring (single-user Nastech memory plugin)."""

import contextlib
import logging
import os
import re
import sqlite3
import threading
import unicodedata
from pathlib import Path

from . import holographic as hrr
from .text import UNSEGMENTED, analyze, name_keys

logger = logging.getLogger(__name__)

# FTS is re-indexed only when indexed text changes: counters, trust and vectors are updated far more often.
_FACTS_AU = """CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE OF content, tags ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    fact_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content         TEXT NOT NULL UNIQUE,
    category        TEXT DEFAULT 'general',
    tags            TEXT DEFAULT '',
    trust_score     REAL DEFAULT 0.5,
    retrieval_count INTEGER DEFAULT 0,
    helpful_count   INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    hrr_vector      BLOB
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    entity_type TEXT DEFAULT 'unknown',
    aliases     TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fact_entities (
    fact_id   INTEGER REFERENCES facts(fact_id),
    entity_id INTEGER REFERENCES entities(entity_id),
    PRIMARY KEY (fact_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_facts_trust    ON facts(trust_score DESC);
CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);
CREATE INDEX IF NOT EXISTS idx_entities_name  ON entities(name);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
    USING fts5(content, tags, content=facts, content_rowid=fact_id);

CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
END;

""" + _FACTS_AU + """

-- 0.4.0. Older versions ignore these tables; see _migrate() for how their writes are reconciled on open.
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS entity_aliases (
    alias_key TEXT PRIMARY KEY,
    entity_id INTEGER NOT NULL REFERENCES entities(entity_id),
    alias     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entity_aliases_entity ON entity_aliases(entity_id);

CREATE TABLE IF NOT EXISTS memory_banks (
    bank_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_name  TEXT NOT NULL UNIQUE,
    vector     BLOB NOT NULL,
    dim        INTEGER NOT NULL,
    fact_count INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

SCHEMA_VERSION = 2  # 1 = 0.1.x-0.3.x layout, 2 = + meta, entities.name_key, entity_aliases
_HELPFUL_DELTA, _UNHELPFUL_DELTA = 0.05, -0.10
_MAX_COUNTED_IDS = 500  # record_retrievals batch cap (far under SQLite's bound-parameter limit)

# Entity extraction, applied in order: capitalized multi-word phrases in any cased script ("John Doe", "Мария
# Иванова"), short quoted terms (straight, curly, guillemets; straight quotes only at word boundaries, so
# contractions like don't / it's never pair up), then both sides of "X aka Y".
_RE_WORD = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*")  # letters only; hyphenated names stay one word
_RE_TOKEN = re.compile(r"\w+(?:-\w+)*")  # aka sides may be handles with digits
_RE_QUOTED = (re.compile(r'(?<!\w)"([^"\n]+)"(?!\w)'), re.compile(r"(?<!\w)'([^'\n]+)'(?!\w)"),
              re.compile(r"«([^»\n]+)»"), re.compile(r"“([^”\n]+)”"),
              re.compile(r"„([^“”\n]+)[“”]"))
_RE_AKA = re.compile(r"\s+(?:aka|a\.k\.a\.|also known as)\s+", re.IGNORECASE)
_RE_NOT_A_NAME = re.compile(r"[=/\\@<>{}\[\]|`]|://|\b\d{1,3}(?:\.\d{1,3}){3}\b")
# A quoted span longer than this is a quotation ("she said: 'see you on Friday'"), not a name.
_QUOTED_MAX_WORDS, _QUOTED_MAX_CHARS = 3, 60
_PHRASE_MAX_WORDS, _ENTITY_MAX_CHARS = 5, 60
# Capitalized only because they open a sentence: dropped from the front of a capitalized run ("So Apple").
_SENTENCE_STARTERS = frozenset("""
    a an and as at but by for from here if in it its my no not now of on or our so that the then there these this
    those to today tomorrow we when where yes yesterday you your
    а в во вот вчера да если завтра и или когда мой моя мои на наш наша наши не но однако по при сегодня так там
    тут у это этот эта эти""".split())
_ENTITY_NAMES_SQL = "SELECT e.name FROM entities e JOIN fact_entities fe ON fe.entity_id = e.entity_id WHERE fe.fact_id = ?"


class DuplicateContentError(ValueError):
    """update_fact would make a fact identical to another one (content is UNIQUE)."""

    def __init__(self, fact_id: int):
        super().__init__(f"another fact already has this content (fact_id {fact_id})")
        self.fact_id = fact_id


def _clamp_trust(value: float) -> float:
    return max(0.0, min(1.0, value))


def fold(text):
    """Comparison key for names: Unicode casefold with whitespace collapsed. SQLite's lower()/LIKE fold ASCII only,
    so this is registered on the connection as hfold() for entity lookups."""
    return " ".join(text.casefold().split()) if isinstance(text, str) else text


def entity_key(name: str) -> str:
    """Identity key of an entity name: NFKC, casefold, hyphens/underscores as spaces, whitespace collapsed, edge
    punctuation stripped. "Nastech Agent", "nastech-agent" and "NASTECH  AGENT" are one entity; "coop" and "co-op" are
    not merged into each other (a hyphen becomes a space, it is not deleted)."""
    text = unicodedata.normalize("NFKC", name or "").casefold().replace("-", " ").replace("_", " ")
    return " ".join(text.split()).strip(" .,;:!?'\"«»“”„")


def _is_capitalized(word: str) -> bool:
    """'John', 'Мария', 'Jean-Luc': each hyphen part an upper-case letter followed by lower-case ones (so not
    'NASA', 'I' or 'iPhone')."""
    return all(len(part) > 1 and part[0].isupper() and part[1:].islower() for part in word.split("-"))


def _capitalized_phrases(text: str) -> list[str]:
    """Names of two to five capitalized words in any cased script, minus a sentence-opening function word."""
    phrases = []
    words_in_order = list(_RE_WORD.finditer(text))
    run: list[re.Match] = []

    def flush():
        words = run[1:] if run and fold(run[0].group(0)) in _SENTENCE_STARTERS else run
        if 2 <= len(words) <= _PHRASE_MAX_WORDS:
            phrases.append(text[words[0].start():words[-1].end()])

    for m in words_in_order:
        if _is_capitalized(m.group(0)) and (not run or text[run[-1].end():m.start()].isspace()):
            run.append(m)
            continue
        flush()
        run = [m] if _is_capitalized(m.group(0)) else []
    flush()
    return phrases


def _aka_side(segment: str, *, from_end: bool) -> str | None:
    """The name right before (from_end) or right after an 'aka': a capitalized run, else a single token."""
    tokens = list(_RE_TOKEN.finditer(segment))
    if not tokens:
        return None
    edge = tokens[-1] if from_end else tokens[0]
    if (edge.end() != len(segment)) if from_end else (edge.start() != 0):
        return None  # punctuation between the name and 'aka'
    picked = [edge]
    if _is_capitalized(edge.group(0)):
        neighbours = reversed(tokens[:-1]) if from_end else tokens[1:]
        for tok in neighbours:
            gap = segment[tok.end():picked[-1].start()] if from_end else segment[picked[-1].end():tok.start()]
            if len(picked) >= _PHRASE_MAX_WORDS or not gap.isspace() or not _is_capitalized(tok.group(0)):
                break
            picked.append(tok)
    if from_end:
        picked.reverse()
    return segment[picked[0].start():picked[-1].end()]


def _valid_entity(name: str) -> bool:
    """Reject what is not a name: too short or long, CLI flags, key=value, paths, URLs, IPs, mostly non-letters."""
    if not 2 <= len(name) <= _ENTITY_MAX_CHARS or name[0] in "-–—" or _RE_NOT_A_NAME.search(name):
        return False
    visible = [c for c in name if not c.isspace()]
    return sum(c.isalpha() for c in visible) >= 0.6 * len(visible)


def _restrict_permissions(db_path: Path) -> None:
    """Owner-only access to the database and its WAL side files (POSIX; best effort)."""
    if os.name != "posix":
        return
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{db_path}{suffix}")
        try:
            if path.exists() and path.stat().st_mode & 0o077:
                os.chmod(path, 0o600)
        except OSError:
            pass


class MemoryStore:
    """SQLite-backed fact store with entity resolution and trust scoring.

    Process-wide shared connection registry: SQLite allows one writer at a time and several providers
    coexist per process (main agent + every delegate_task subagent), so all instances for the same database
    share ONE connection and ONE re-entrant lock — writes are fully serialized and "database is locked" is
    impossible within a process. Refcounted: closing one instance never tears the connection out from under a
    sibling. Every multi-statement write runs in one transaction, so a failure leaves nothing half-written."""

    _shared: dict = {}
    _shared_guard = threading.Lock()

    def __init__(self, db_path: "str | Path | None" = None, default_trust: float = 0.5, hrr_dim: int = 1024, *,
                 migrate: bool = True) -> None:
        if db_path is None:
            from nastech_constants import get_nastech_home
            db_path = str(get_nastech_home() / "memory_store.db")
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.default_trust, self.hrr_dim, self._hrr_available = _clamp_trust(default_trust), hrr_dim, hrr._HAS_NUMPY
        try:  # resolve() so symlinked/relative paths to the same file share ONE connection
            self._key = str(self.db_path.resolve())
        except OSError:
            self._key = str(self.db_path)
        with MemoryStore._shared_guard:
            entry = MemoryStore._shared.get(self._key)
            if entry is None:
                # Autocommit outside explicit transactions (_tx): a failed statement never leaves a dangling
                # transaction holding the write lock.
                conn = sqlite3.connect(self._key, check_same_thread=False, timeout=10.0, isolation_level=None)
                conn.row_factory = sqlite3.Row
                conn.create_function("hfold", 1, fold, deterministic=True)
                entry = MemoryStore._shared[self._key] = {"conn": conn, "lock": threading.RLock(), "refs": 0, "ready": False,
                                                          "migrated": False, "generation": 0}
            entry["refs"] += 1
            self._entry, self._conn, self._lock = entry, entry["conn"], entry["lock"]
        with self._lock:  # schema initialised once per shared connection; migrated by the first writer
            try:
                if not entry["ready"]:
                    self._init_db()
                    entry["ready"] = True
                if migrate and not entry["migrated"]:
                    self._migrate()
                    entry["migrated"] = True
                elif migrate:
                    self._check_vectors()
            except BaseException:
                self.close()  # drop our reference; a broken connection must not stay registered
                raise
        _restrict_permissions(self.db_path)

    def _init_db(self) -> None:
        """Create schema, enable WAL via the shared fallback helper (NFS/SMB/FUSE degrade gracefully), add hrr_vector
        to pre-HRR DBs, and narrow the FTS update trigger of DBs created by older versions."""
        from nastech_state_wal import apply_wal_with_fallback
        apply_wal_with_fallback(self._conn, db_label="memory_store.db (holographic)")
        self._conn.executescript(_SCHEMA)
        if "hrr_vector" not in {row[1] for row in self._conn.execute("PRAGMA table_info(facts)").fetchall()}:
            from nastech_cli.sqlite_util import add_column_if_missing
            add_column_if_missing(self._conn, "facts", "hrr_vector", "hrr_vector BLOB")
        trigger = self._one("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'facts_au'")
        if trigger is not None and "UPDATE OF" not in (trigger["sql"] or "").upper():
            with self._tx():
                self._conn.execute("DROP TRIGGER facts_au")
                self._conn.execute(_FACTS_AU)

    def _migrate(self) -> None:
        """Bring the store to SCHEMA_VERSION, idempotently, on every open. Additive only, so every older version
        keeps working on the file; what an older version wrote in between is reconciled here:
        - entities it created have no ``name_key``: keys are filled in, and entities that turn out to share a key
          are merged (links and aliases moved to the oldest one);
        - aliases it may have stored in the legacy ``entities.aliases`` column are copied into ``entity_aliases``;
        - HRR vectors are re-encoded when ``hrr_dim`` differs from the dimension they were written with (recorded in
          ``meta``; before 0.4.0, inferred from the stored blobs), so a changed setting no longer breaks queries."""
        with self._tx():
            if "name_key" not in {r[1] for r in self._conn.execute("PRAGMA table_info(entities)")}:
                self._conn.execute("ALTER TABLE entities ADD COLUMN name_key TEXT")
            # Aliases an older version deleted the entity of, or that point nowhere: drop before anything resolves them.
            self._conn.execute("DELETE FROM entity_aliases WHERE entity_id NOT IN (SELECT entity_id FROM entities)")
            # Legacy comma-separated aliases are imported BEFORE keys are reconciled: a merge deletes the source row.
            names = {entity_key(r["name"]) for r in self._conn.execute("SELECT name FROM entities")}
            for row in self._conn.execute("SELECT entity_id, aliases FROM entities WHERE aliases IS NOT NULL AND aliases != ''").fetchall():
                for alias in row["aliases"].split(","):
                    key = entity_key(alias)
                    if key and key not in names:
                        self._conn.execute("INSERT OR IGNORE INTO entity_aliases (alias_key, entity_id, alias) VALUES (?, ?, ?)",
                                           (key, row["entity_id"], alias.strip()))
            self._reconcile_entity_keys()
            self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_key ON entities(name_key) WHERE name_key IS NOT NULL")
            self._sync_hrr_dim()
            self._bump()
            self._conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                               "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(SCHEMA_VERSION),))

    def _reconcile_entity_keys(self) -> None:
        """Fill missing name keys; merge entities whose keys collide into the oldest of them."""
        rows = self._conn.execute("SELECT entity_id, name, name_key, entity_type FROM entities ORDER BY entity_id").fetchall()
        if all(r["name_key"] for r in rows):
            return
        canonical: dict[str, int] = {}
        for r in rows:
            if r["name_key"]:
                canonical.setdefault(r["name_key"], r["entity_id"])
        for r in rows:
            if r["name_key"]:
                continue
            key = entity_key(r["name"]) or f"#{r['entity_id']}"
            keep = canonical.get(key)
            if keep is None:
                canonical[key] = r["entity_id"]
                self._conn.execute("UPDATE entities SET name_key = ? WHERE entity_id = ?", (key, r["entity_id"]))
            else:
                self._merge_into(r["entity_id"], keep, declared=r["entity_type"] == "declared")

    def _merge_into(self, source: int, target: int, *, declared: bool | None = None) -> None:
        """Make entity *source* part of *target* (inside a transaction): links, aliases (both representations) and
        the declared flag move, *source*'s own name stays findable as an alias, the vectors of every affected fact are
        re-encoded with the surviving name, and category banks are dropped."""
        src = self._one("SELECT name, name_key, aliases, entity_type FROM entities WHERE entity_id = ?", (source,))
        tgt = self._one("SELECT name, name_key, aliases FROM entities WHERE entity_id = ?", (target,))
        if src is None or tgt is None or source == target:
            return
        affected = [r[0] for r in self._conn.execute("SELECT fact_id FROM fact_entities WHERE entity_id IN (?, ?)", (source, target))]
        self._conn.execute("INSERT OR IGNORE INTO fact_entities (fact_id, entity_id) SELECT fact_id, ? FROM fact_entities "
                           "WHERE entity_id = ?", (target, source))
        self._conn.execute("DELETE FROM fact_entities WHERE entity_id = ?", (source,))
        self._conn.execute("UPDATE OR IGNORE entity_aliases SET entity_id = ? WHERE entity_id = ?", (target, source))
        self._conn.execute("DELETE FROM entity_aliases WHERE entity_id = ?", (source,))
        target_key = tgt["name_key"] or entity_key(tgt["name"])
        moved = [a for a in (src["aliases"] or "").split(",") if a.strip()]
        if entity_key(src["name"]) != target_key:
            moved.append(src["name"])
            self._conn.execute("INSERT OR IGNORE INTO entity_aliases (alias_key, entity_id, alias) VALUES (?, ?, ?)",
                               (entity_key(src["name"]), target, src["name"]))
        legacy = [a for a in (tgt["aliases"] or "").split(",") if a.strip()]
        known = {entity_key(a) for a in legacy} | {target_key}
        legacy += [a.strip() for a in moved if entity_key(a) not in known]
        self._conn.execute("UPDATE entities SET aliases = ? WHERE entity_id = ?", (",".join(dict.fromkeys(legacy)), target))
        if declared if declared is not None else src["entity_type"] == "declared":
            self._conn.execute("UPDATE entities SET entity_type = 'declared' WHERE entity_id = ?", (target,))
        self._conn.execute("DELETE FROM entities WHERE entity_id = ?", (source,))
        for fact_id in affected:
            row = self._one("SELECT content FROM facts WHERE fact_id = ?", (fact_id,))
            if row is not None:
                self._compute_hrr_vector(fact_id, row["content"])
        self._conn.execute("DELETE FROM memory_banks")
        self._bump()

    def _meta(self, key: str) -> str | None:
        row = self._one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    def _bump(self) -> None:
        """Invalidate per-connection caches (the known-entity index) after an entity change or a rollback."""
        self._entry["generation"] = self._entry.get("generation", 0) + 1

    def _check_vectors(self) -> None:
        """Re-encode vectors whose size does not match hrr_dim (another version or setting wrote them)."""
        if not self._hrr_available:
            return
        sizes = (4 + 4 * self.hrr_dim, 8 * self.hrr_dim)
        stale = self._conn.execute("SELECT fact_id, content FROM facts WHERE hrr_vector IS NULL OR LENGTH(hrr_vector) NOT IN (?, ?)",
                                   sizes).fetchall()
        if stale:
            with self._tx():
                for row in stale:
                    self._compute_hrr_vector(row["fact_id"], row["content"])

    def _sync_hrr_dim(self) -> None:
        """Record hrr_dim; re-encode every vector when the configured dimension differs from the stored one."""
        if not self._hrr_available:
            return
        stored = self._meta("hrr_dim")
        if stored is None:  # before 0.4.0: infer from a stored blob (prefixed float32 or legacy float64)
            blob = self._one("SELECT hrr_vector FROM facts WHERE hrr_vector IS NOT NULL LIMIT 1")
            if blob is not None:
                data = bytes(blob["hrr_vector"])
                stored = str((len(data) - 4) // 4 if data.startswith(b"HRR1") else len(data) // 8)
        if stored is not None and stored != str(self.hrr_dim):
            logger.warning("holographic: hrr_dim changed (%s -> %s), re-encoding stored vectors", stored, self.hrr_dim)
            for row in self._conn.execute("SELECT fact_id, content FROM facts").fetchall():
                self._compute_hrr_vector(row["fact_id"], row["content"])
            self._conn.execute("DELETE FROM memory_banks")
        self._check_vectors()  # the record is not proof: an older writer may have stored other sizes since
        self._conn.execute("INSERT INTO meta (key, value) VALUES ('hrr_dim', ?) "
                           "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(self.hrr_dim),))

    def _one(self, sql: str, params=()):
        return self._conn.execute(sql, params).fetchone()

    def _write(self, sql: str, params=()) -> sqlite3.Cursor:
        """One write statement: autocommitted on its own, or part of the enclosing _tx()."""
        return self._conn.execute(sql, params)

    @contextlib.contextmanager
    def _tx(self):
        """One write transaction (BEGIN IMMEDIATE ... COMMIT, ROLLBACK on error); nested calls join the outer one."""
        with self._lock:
            if self._conn.in_transaction:
                yield
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                self._bump()  # anything cached inside the transaction never happened
                raise
            self._conn.execute("COMMIT")

    def read(self, sql: str, params=()) -> list:
        """Run a read under the store lock (a consistent snapshot against this process's writers)."""
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def add_fact(self, content: str, category: str = "general", tags: str = "") -> int:
        """Insert a fact and return its fact_id; on duplicate content (UNIQUE) return the existing fact_id untouched."""
        return self.add_fact_status(content, category=category, tags=tags)[0]

    def add_fact_status(self, content: str, category: str = "general", tags: str = "", *,
                        trust: float | None = None, entities=()) -> tuple[int, bool]:
        """(fact_id, created). Links extracted, declared (*entities*) and already known entities and stores the HRR
        vector in the same transaction. *trust* overrides the default trust of a new fact."""
        content = (content or "").strip()
        if not content:
            raise ValueError("content must not be empty")
        with self._tx():
            try:
                fact_id: int = self._write(
                    "INSERT INTO facts (content, category, tags, trust_score) VALUES (?, ?, ?, ?)",
                    (content, category, tags, _clamp_trust(self.default_trust if trust is None else trust))).lastrowid  # type: ignore[assignment]
            except sqlite3.IntegrityError:
                return int(self._one("SELECT fact_id FROM facts WHERE content = ?", (content,))["fact_id"]), False
            self._link_entities(fact_id, content, entities)
            self._compute_hrr_vector(fact_id, content)
            self._invalidate_bank(category)
            return fact_id, True

    def replace_fact(self, old_id: int, content: str, category: str = "general", tags: str = "", *,
                     entities=()) -> tuple[int, bool]:
        """Supersede fact *old_id* with *content* in one transaction: the new fact is stored (or found, if that text
        already exists) and the old one removed. Returns (fact_id, created). KeyError if *old_id* does not exist.
        The new fact starts at the default trust: a correction has not earned the old fact's feedback."""
        with self._tx():
            old = self._one("SELECT fact_id, content FROM facts WHERE fact_id = ?", (old_id,))
            if old is None:
                raise KeyError(f"fact_id {old_id} not found")
            if old["content"] == (content or "").strip():
                return int(old_id), False
            fact_id, created = self.add_fact_status(content, category=category, tags=tags, entities=entities)
            self.remove_fact(old_id)
            return fact_id, created

    def update_fact(self, fact_id: int, content: str | None = None, trust_delta: float | None = None,
                    tags: str | None = None, category: str | None = None, *, entities=(), touch: bool = True) -> bool:
        """Partially update a fact (trust clamped to [0, 1]). Returns True if the row existed. Raises ValueError for
        empty content and DuplicateContentError when another fact already has the new content. touch=False keeps
        updated_at (temporal decay and "recently changed" read it): for search aids that do not change the fact."""
        if content is not None:
            content = content.strip()
            if not content:
                raise ValueError("content must not be empty")
        with self._tx():
            row = self._one("SELECT fact_id, trust_score, category FROM facts WHERE fact_id = ?", (fact_id,))
            if row is None:
                return False
            changes = {col: val for col, val in {
                "content": content, "tags": tags, "category": category,
                "trust_score": _clamp_trust(row["trust_score"] + trust_delta) if trust_delta is not None else None,
            }.items() if val is not None}
            assignments = ", ".join((["updated_at = CURRENT_TIMESTAMP"] if touch else []) + [f"{col} = ?" for col in changes])
            if not assignments:
                return True
            try:
                self._write(f"UPDATE facts SET {assignments} WHERE fact_id = ?", [*changes.values(), fact_id])
            except sqlite3.IntegrityError:
                other = self._one("SELECT fact_id FROM facts WHERE content = ?", (content,))
                raise DuplicateContentError(int(other["fact_id"]) if other else -1) from None
            if content is not None:  # re-extract entities (declared links survive a rewording) and recompute the vector
                kept = [r[0] for r in self._conn.execute(
                    "SELECT e.name FROM fact_entities fe JOIN entities e ON e.entity_id = fe.entity_id "
                    "WHERE fe.fact_id = ? AND e.entity_type = 'declared'", (fact_id,))]
                self._write("DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,))
                self._link_entities(fact_id, content, [*kept, *(entities or ())])
                self._compute_hrr_vector(fact_id, content)
                self._drop_orphan_entities()
            elif entities:  # declare who the fact is about without rewording it
                current = self._one("SELECT content FROM facts WHERE fact_id = ?", (fact_id,))["content"]
                self._link_declared(fact_id, entities)
                self._compute_hrr_vector(fact_id, current)
            self._invalidate_bank(row["category"])
            if category is not None and category != row["category"]:
                self._invalidate_bank(category)
            return True

    def remove_fact(self, fact_id: int) -> bool:
        """Delete a fact, its entity links and entities left without facts. Returns True if the row existed."""
        with self._tx():
            row = self._one("SELECT fact_id, category FROM facts WHERE fact_id = ?", (fact_id,))
            if row is None:
                return False
            self._write("DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,))
            self._write("DELETE FROM facts WHERE fact_id = ?", (fact_id,))
            self._drop_orphan_entities()
            self._invalidate_bank(row["category"])
            return True

    def list_facts(self, category: str | None = None, min_trust: float = 0.0, limit: int = 50) -> list[dict]:
        """Browse facts ordered by trust_score descending, optionally filtered by category / min trust."""
        category_clause = "AND category = ? " if category is not None else ""
        params = [min_trust] + ([category] if category is not None else []) + [limit]
        sql = ("SELECT fact_id, content, category, tags, trust_score, retrieval_count, helpful_count, "
               f"created_at, updated_at FROM facts WHERE trust_score >= ? {category_clause}"
               "ORDER BY trust_score DESC LIMIT ?")
        return [dict(r) for r in self.read(sql, params)]

    def count_facts(self) -> int:
        return int(self.read("SELECT COUNT(*) FROM facts")[0][0])

    def record_retrievals(self, fact_ids) -> int:
        """retrieval_count += 1 for facts handed to the model; returns the number of rows touched.

        Only the counter changes: ``updated_at`` drives temporal decay and recall is not an edit (and the FTS
        trigger ignores the column). Only integer ids count (``True`` and ``1.9`` do not); duplicates count once."""
        ids = sorted({int(raw) for raw in fact_ids or ()
                      if (isinstance(raw, int) and not isinstance(raw, bool)) or (isinstance(raw, str) and raw.isdigit())})
        ids = ids[:_MAX_COUNTED_IDS]
        if not ids:
            return 0
        with self._lock:
            cur = self._write(f"UPDATE facts SET retrieval_count = retrieval_count + 1 "
                              f"WHERE fact_id IN ({','.join('?' * len(ids))})", ids)
            return max(cur.rowcount, 0)

    def add_retrieval_counts(self, counts: dict) -> bool:
        """Add buffered recall counts ({fact_id: n}) in one transaction without waiting on another process's write
        lock (busy timeout 0 for this write only). False when the database was busy: the caller keeps the counts."""
        rows = [(int(n), int(i)) for i, n in counts.items()
                if isinstance(i, int) and not isinstance(i, bool) and isinstance(n, int) and n > 0]
        if not rows:
            return True
        with self._lock:
            self._conn.execute("PRAGMA busy_timeout = 0")
            try:
                with self._tx():
                    self._conn.executemany("UPDATE facts SET retrieval_count = retrieval_count + ? WHERE fact_id = ?", rows)
                return True
            except sqlite3.OperationalError as exc:
                logger.debug("holographic: recall counts deferred (%s)", exc)
                return False
            finally:
                self._conn.execute("PRAGMA busy_timeout = 10000")

    def record_feedback(self, fact_id: int, helpful: bool) -> dict:
        """Adjust trust asymmetrically: helpful -> +0.05 and helpful_count += 1; unhelpful -> -0.10.
        Returns {fact_id, old_trust, new_trust, helpful_count}. Raises KeyError if fact_id is unknown."""
        with self._tx():
            row = self._one("SELECT fact_id, trust_score, helpful_count FROM facts WHERE fact_id = ?", (fact_id,))
            if row is None:
                raise KeyError(f"fact_id {fact_id} not found")
            old_trust: float = row["trust_score"]
            new_trust = _clamp_trust(old_trust + (_HELPFUL_DELTA if helpful else _UNHELPFUL_DELTA))
            increment = 1 if helpful else 0
            self._write("UPDATE facts SET trust_score = ?, helpful_count = helpful_count + ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE fact_id = ?", (new_trust, increment, fact_id))
            return {"fact_id": fact_id, "old_trust": old_trust, "new_trust": new_trust, "helpful_count": row["helpful_count"] + increment}

    def entity_ids(self, name: str) -> list[int]:
        """Entities whose name or an alias has the identity key of *name* (see entity_key). Entities an older
        version created since this store was opened have no key yet; they are matched by casefolded name."""
        key = entity_key(name or "")
        if not key:
            return []
        with self._lock:
            if not self._entry.get("migrated"):  # opened read-only on a store no writer has migrated yet
                return sorted({r[0] for r in self._conn.execute("SELECT entity_id FROM entities WHERE hfold(name) = ?", (fold(name),))})
            ids = {r[0] for r in self._conn.execute(
                "SELECT entity_id FROM entities WHERE name_key = ? "
                "UNION SELECT ea.entity_id FROM entity_aliases ea JOIN entities e ON e.entity_id = ea.entity_id WHERE ea.alias_key = ? "
                "UNION SELECT entity_id FROM entities WHERE name_key IS NULL AND hfold(name) = ?", (key, key, fold(name)))}
        return sorted(ids)

    def add_alias(self, name: str, alias: str) -> dict:
        """Make *alias* another name of the entity *name* (created if needed). If *alias* is already an entity of its
        own, it is merged into *name*: links and aliases move, and its name becomes an alias."""
        key, alias_key = entity_key(name), entity_key(alias)
        if not key or not alias_key or key == alias_key:
            raise ValueError("an alias needs two different non-empty names")
        with self._tx():
            target = self._resolve_entity(name.strip())
            owner = self._one("SELECT ea.entity_id, e.name FROM entity_aliases ea JOIN entities e ON e.entity_id = ea.entity_id "
                              "WHERE ea.alias_key = ?", (alias_key,))
            if owner is not None and owner["entity_id"] != target:
                raise ValueError(f"'{alias.strip()}' is already an alias of '{owner['name']}'; merge the two entities "
                                 f"(alias '{name.strip()}' -> '{owner['name']}') if they are the same")
            other = [r[0] for r in self._conn.execute("SELECT entity_id FROM entities WHERE name_key = ? AND entity_id != ?",
                                                      (alias_key, target))]
            for source in other:
                self._merge_into(source, target)
            self._conn.execute("INSERT INTO entity_aliases (alias_key, entity_id, alias) VALUES (?, ?, ?) "
                               "ON CONFLICT(alias_key) DO UPDATE SET entity_id = excluded.entity_id, alias = excluded.alias",
                               (alias_key, target, alias.strip()))
            # Older versions resolve aliases from the comma-separated column: keep it in step.
            row = self._one("SELECT aliases FROM entities WHERE entity_id = ?", (target,))
            legacy = [a for a in (row["aliases"] or "").split(",") if a.strip()]
            if alias_key not in {entity_key(a) for a in legacy}:
                self._conn.execute("UPDATE entities SET aliases = ? WHERE entity_id = ?", (",".join(legacy + [alias.strip()]), target))
            self._bump()
            return {"entity_id": target, "alias": alias.strip(), "merged": other}

    def list_entities(self, limit: int = 50) -> list[dict]:
        """Entities with their aliases and number of facts, most linked first."""
        rows = self.read("SELECT e.entity_id, e.name, e.entity_type, COUNT(fe.fact_id) AS facts FROM entities e "
                         "LEFT JOIN fact_entities fe ON fe.entity_id = e.entity_id GROUP BY e.entity_id "
                         "ORDER BY facts DESC, e.name LIMIT ?", (limit,))
        aliases: dict[int, list[str]] = {}
        for r in self.read("SELECT entity_id, alias FROM entity_aliases ORDER BY alias"):
            aliases.setdefault(r["entity_id"], []).append(r["alias"])
        return [{"entity_id": r["entity_id"], "name": r["name"], "facts": r["facts"], "declared": r["entity_type"] == "declared",
                 "aliases": aliases.get(r["entity_id"], [])} for r in rows]

    def _extract_entities(self, text: str) -> list[str]:
        """Entity candidates (see the pattern table) that pass _valid_entity, deduplicated by casefold in first-seen
        order."""
        raw = _capitalized_phrases(text) + [
            m.group(1) for pattern in _RE_QUOTED for m in pattern.finditer(text)
            if len(m.group(1)) <= _QUOTED_MAX_CHARS and len(m.group(1).split()) <= _QUOTED_MAX_WORDS]
        for left, right in self._aka_pairs(text):
            raw += [left, right]
        uniq: dict[str, str] = {}  # casefolded key -> first-seen spelling, insertion-ordered
        for name in filter(None, (n.strip(" .,;:!?") for n in raw if n)):
            if _valid_entity(name):
                uniq.setdefault(fold(name), name)
        return list(uniq.values())

    @staticmethod
    def _aka_pairs(text: str) -> list[tuple[str, str]]:
        """(name, alias) for every "X aka Y" in *text* whose sides are valid names."""
        pairs = []
        for m in _RE_AKA.finditer(text):
            left, right = _aka_side(text[:m.start()], from_end=True), _aka_side(text[m.end():], from_end=False)
            left, right = (n.strip(" .,;:!?") if n else "" for n in (left, right))
            if _valid_entity(left) and _valid_entity(right) and entity_key(left) != entity_key(right):
                pairs.append((left, right))
        return pairs

    def _link_entities(self, fact_id: int, content: str, declared=()) -> None:
        """Link the fact to its entities: names extracted from the text, names the caller declared (a one-word name
        such as "Masha" is accepted here), and entities already known that the text mentions in any word form.
        "X aka Y" makes Y an alias of X rather than a second entity (unless Y is already an entity)."""
        pairs = self._aka_pairs(content)
        alias_only = {entity_key(right) for _, right in pairs}
        ids = {self._resolve_entity(name) for name in self._extract_entities(content)
               if entity_key(name) not in alias_only or self.entity_ids(name)}
        for left, right in pairs:
            if not self.entity_ids(right):
                ids.add(self.add_alias(left, right)["entity_id"])
        ids |= self.mentioned_entity_ids(content)
        for entity_id in sorted(ids):
            self._write("INSERT OR IGNORE INTO fact_entities (fact_id, entity_id) VALUES (?, ?)", (fact_id, entity_id))
        self._link_declared(fact_id, declared)

    def _link_declared(self, fact_id: int, declared) -> None:
        """Link names the caller declared (one-word names allowed) and mark them declared."""
        for name in declared or ():
            name = name.strip(" .,;:!?") if isinstance(name, str) else ""
            if _valid_entity(name):
                entity_id = self._resolve_entity(name)
                if self._write("UPDATE entities SET entity_type = 'declared' WHERE entity_id = ? AND entity_type != 'declared'",
                               (entity_id,)).rowcount:
                    self._bump()
                self._write("INSERT OR IGNORE INTO fact_entities (fact_id, entity_id) VALUES (?, ?)", (fact_id, entity_id))

    def _known_entities(self) -> dict[str, list[tuple[int, tuple]]]:
        """{key of the first word: [(entity_id, key sets of a name's words)]} for entities safe to match in free text:
        multi-word names, and one-word names only when declared (older stores hold one-word junk such as a quoted
        "Done", which would otherwise attach itself to every fact saying "done"). Cached per connection and rebuilt
        after any entity change here, a rollback, or a commit by another connection (PRAGMA data_version)."""
        with self._lock:
            version = (self._entry.get("generation", 0), self._one("PRAGMA data_version")[0])
            cached = self._entry.get("entities")
            if cached is None or cached[0] != version:
                index: dict[str, list[tuple[int, tuple]]] = {}
                names: dict[int, list[str]] = {}
                if self._entry.get("migrated"):
                    for row in self._conn.execute("SELECT entity_id, alias FROM entity_aliases"):
                        names.setdefault(row["entity_id"], []).append(row["alias"])
                for row in self._conn.execute("SELECT entity_id, name, aliases, entity_type FROM entities"):
                    for name in [row["name"], *((row["aliases"] or "").split(",")), *names.get(row["entity_id"], [])]:
                        key = name_keys(name)
                        if len(key) > 1 or (key and row["entity_type"] == "declared"):
                            for first in key[0][0]:
                                index.setdefault(first, []).append((int(row["entity_id"]), key))
                        glued = name.strip().lower()
                        if UNSEGMENTED.search(glued) and len(glued) >= 2 and (len(key) > 1 or row["entity_type"] == "declared"):
                            index.setdefault("", []).append((int(row["entity_id"]), glued))  # "" = substring names
                cached = self._entry["entities"] = (version, index)
            return cached[1]

    def mentioned_entity_ids(self, text: str) -> set[int]:
        """Known entities whose name (or alias) occurs in *text* as a run of words: a Latin-script name as written
        (or with 's), a name in another script by its keys, so "Маши" and "Машу" mention "Маша" and "لإيفا" mentions
        "إيفا", while "jeans" does not mention "Jean" and "машина" not "Маша". Looked up by each word's keys, not by
        scanning names."""
        sequence = analyze(text).words
        index = self._known_entities()
        found = set()
        for i, word in enumerate(sequence):
            for first in word.keys:
                for entity_id, key in index.get(first, ()):
                    if entity_id not in found and len(sequence) - i >= len(key) and all(
                            sequence[i + j].text in forms if exact else not forms.isdisjoint(sequence[i + j].keys)
                            for j, (forms, exact) in enumerate(key)):
                        found.add(entity_id)
        if UNSEGMENTED.search(text or ""):  # a name in Chinese/Thai text has no spaces around it: substring match
            lowered = text.lower()
            for entity_id, name in index.get("", ()):
                if name in lowered:
                    found.add(entity_id)
        return found

    def _resolve_entity(self, name: str) -> int:
        """Return the entity_id for a casefold name or alias match, creating the entity if absent."""
        ids = self.entity_ids(name)
        if ids:
            return ids[0]
        self._bump()
        if not self._entry.get("migrated"):
            return int(self._write("INSERT INTO entities (name) VALUES (?)", (name,)).lastrowid)  # type: ignore[arg-type]
        return int(self._write("INSERT INTO entities (name, name_key) VALUES (?, ?)", (name, entity_key(name))).lastrowid)  # type: ignore[arg-type]

    def _drop_orphan_entities(self) -> None:
        if self._write("DELETE FROM entities WHERE entity_id NOT IN (SELECT entity_id FROM fact_entities)").rowcount:
            self._bump()
        if self._entry.get("migrated"):
            self._write("DELETE FROM entity_aliases WHERE entity_id NOT IN (SELECT entity_id FROM entities)")

    def _compute_hrr_vector(self, fact_id: int, content: str) -> None:
        """Compute and store the HRR vector for a fact (linked entities as roles). No-op without numpy."""
        if not self._hrr_available:
            return
        entities = [row["name"] for row in self._conn.execute(_ENTITY_NAMES_SQL, (fact_id,)).fetchall()]
        blob = hrr.phases_to_bytes(hrr.encode_fact(content, entities, self.hrr_dim))
        self._write("UPDATE facts SET hrr_vector = ? WHERE fact_id = ?", (blob, fact_id))

    def _invalidate_bank(self, category: str | None) -> None:
        """Drop the category's memory bank. Retrieval no longer reads banks; deleting (instead of rebuilding the
        whole category on every write) keeps a downgrade safe: an older version that finds no bank falls back to
        the per-fact vectors, and rebuilds the bank itself on its next write."""
        if category is not None:
            self._write("DELETE FROM memory_banks WHERE bank_name = ?", (f"cat:{category}",))

    @classmethod
    def release_all_under(cls, directory: "str | Path") -> int:
        """Force-close every shared connection whose database lives under ``directory``; returns the count.
        close() is refcount-driven, so a live holder (e.g. an agent's provider) keeps a profile's SQLite handle
        open, which on Windows makes rmtree of the profile fail. The directory is going away, so later use by a
        stale holder is expected to fail.

        That is exactly what a profile delete must break on Windows: the desktop's main ``serve`` process
        opens ``memory_store.db`` for every known profile, and ``rmtree`` of the profile directory fails
        with ``WinError 32`` while any of those handles is open (#88347). In a process that holds none (e.g.
        the CLI deleting from outside serve) this is a harmless no-op returning 0.
        """
        root = os.path.normcase(str(Path(directory).expanduser().resolve())) + os.sep
        with cls._shared_guard:
            doomed = [cls._shared.pop(key) for key in list(cls._shared) if os.path.normcase(key).startswith(root)]
            for entry in doomed:
                try:
                    with entry["lock"]:
                        entry["conn"].close()
                except Exception:
                    pass  # an already-closed/broken connection must not abort releasing siblings
        return len(doomed)

    def close(self) -> None:
        """Release this instance's reference; the connection closes with the last holder. Idempotent."""
        with MemoryStore._shared_guard:
            entry = getattr(self, "_entry", None)
            if entry is None:
                return
            entry["refs"] -= 1
            if entry["refs"] <= 0:
                try:
                    entry["conn"].close()
                finally:
                    # Pop only OUR entry: after release_all_under() a same-path store may have
                    # registered a FRESH entry under this key; a stale late close() must not evict it.
                    # See #88347.
                    if MemoryStore._shared.get(self._key) is entry:
                        MemoryStore._shared.pop(self._key, None)
            self._entry = None

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

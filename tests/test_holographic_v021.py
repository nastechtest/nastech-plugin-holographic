"""0.2.1: entity recall from the entity index (strict AND for reason), trust filters on every path, atomic writes,
honest add/update statuses, cleaner entity extraction, read-only agent contexts, the mirror contract, setup defaults."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("numpy")

from nastech_plugin_holographic import HolographicMemoryProvider
from nastech_plugin_holographic.retrieval import FactRetriever
from nastech_plugin_holographic.store import DuplicateContentError, MemoryStore

DIM = 1024


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(str(tmp_path / "facts.db"), hrr_dim=DIM)
    yield s
    s.close()


def _retriever(store):
    return FactRetriever(store, hrr_dim=DIM)


def _provider(tmp_path, **kwargs):
    config = {"db_path": str(tmp_path / "memory_store.db"), "hrr_dim": DIM, **kwargs.pop("config", {})}
    provider = HolographicMemoryProvider(config=config)
    provider.initialize(session_id="s", **kwargs)
    return provider


def _call(provider, **args):
    return json.loads(provider.handle_tool_call("fact_store", args))


# --- entity recall ----------------------------------------------------------------------------------------------


def test_reason_never_returns_facts_about_only_one_entity(store):
    store.add_fact("Alice Smith lives in Paris")
    store.add_fact("Bob Brown lives in Rome")
    assert _retriever(store).reason(["Alice Smith", "Bob Brown"]) == []
    both = store.add_fact("Alice Smith and Bob Brown share a flat")
    assert [r["fact_id"] for r in _retriever(store).reason(["alice smith", "BOB BROWN"])] == [both]


def test_reason_with_an_unknown_name_still_requires_every_name(store):
    store.add_fact("Alice Smith met masha at the market")
    store.add_fact("Alice Smith lives in Paris")
    results = _retriever(store).reason(["Alice Smith", "masha"])
    assert [r["content"] for r in results] == ["Alice Smith met masha at the market"]


def test_probe_of_an_unknown_name_returns_nothing_unrelated(store):
    store.add_fact("Alice Smith lives in Paris")
    assert _retriever(store).probe("Unknown Person 19293") == []


def test_probe_matches_cyrillic_names_in_any_case(store):
    fact = store.add_fact("Мария Иванова работает в Сбере")
    assert [r["fact_id"] for r in _retriever(store).probe("МАРИЯ ИВАНОВА")] == [fact]


def test_trust_filter_applies_to_probe_related_reason_and_contradict(store):
    low = store.add_fact("Alice Smith lives in Paris")
    store.add_fact("Alice Smith lives in London")
    store._write("UPDATE facts SET trust_score = 0.1 WHERE fact_id = ?", (low,))
    r = _retriever(store)
    for results in (r.probe("Alice Smith"), r.related("Alice Smith"), r.reason(["Alice Smith"])):
        assert low not in {x["fact_id"] for x in results}
    assert r.contradict(min_trust=0.3) == []


def test_related_has_a_noise_floor(store):
    store.add_fact("Alice Smith lives in Paris")
    assert _retriever(store).related("Unknown Person 19293") == []


# --- contradict -------------------------------------------------------------------------------------------------


def test_contradict_ignores_a_claim_that_only_adds_detail(store):
    store.add_fact("Alice Smith likes apples")
    store.add_fact("Alice Smith likes apples and bananas")
    assert _retriever(store).contradict() == []


# --- entity extraction ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("don't stop it's fine", []),
    ("Alice Smith aka Bob Brown works here", ["Alice Smith", "Bob Brown"]),
    ('run with "--flag" and "key=value"', []),
    ("So Apple released a phone", []),
    ("Yesterday Alice Smith called", ["Alice Smith"]),
    ("server at '10.0.0.1'", []),
    ("user42 aka bob", ["user42", "bob"]),
])
def test_entity_extraction_rejects_noise(store, text, expected):
    assert store._extract_entities(text) == expected


# --- writes -----------------------------------------------------------------------------------------------------


def test_add_reports_an_existing_fact(tmp_path):
    provider = _provider(tmp_path)
    first = _call(provider, action="add", content="Alice Smith lives in Paris")
    again = _call(provider, action="add", content="  Alice Smith lives in Paris ")
    assert first["status"] == "added" and again == {"fact_id": first["fact_id"], "status": "exists"}
    provider.shutdown()


def test_update_to_existing_content_names_the_duplicate(tmp_path):
    provider = _provider(tmp_path)
    a = _call(provider, action="add", content="Alice Smith lives in Paris")["fact_id"]
    b = _call(provider, action="add", content="Alice Smith lives in London")["fact_id"]
    result = _call(provider, action="update", fact_id=b, content="Alice Smith lives in Paris")
    assert result["updated"] is False and result["duplicate_of"] == a
    provider.shutdown()


def test_update_rejects_blank_content(store):
    fact = store.add_fact("Alice Smith lives in Paris")
    with pytest.raises(ValueError):
        store.update_fact(fact, content="   ")
    with pytest.raises(DuplicateContentError):
        store.update_fact(store.add_fact("Bob Brown lives in Rome"), content="Alice Smith lives in Paris")


def test_failed_update_leaves_the_fact_untouched(store, monkeypatch):
    fact = store.add_fact("Alice Smith lives in Paris")
    monkeypatch.setattr(MemoryStore, "_compute_hrr_vector", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        store.update_fact(fact, content="Alice Smith lives in Rome")
    monkeypatch.undo()
    assert [f["content"] for f in store.list_facts()] == ["Alice Smith lives in Paris"]
    assert [r["fact_id"] for r in _retriever(store).search("Paris", min_trust=0)] == [fact]


def test_removing_the_last_fact_of_an_entity_drops_the_entity(store):
    fact = store.add_fact("Alice Smith lives in Paris")
    store.remove_fact(fact)
    assert store.read("SELECT COUNT(*) FROM entities")[0][0] == 0


def test_record_retrievals_counts_integers_only(store):
    fact = store.add_fact("Alice Smith lives in Paris")
    assert store.record_retrievals([1.9, True, "x", None]) == 0
    assert store.record_retrievals([fact, str(fact)]) == 1


def test_counter_updates_do_not_touch_the_fts_index(store):
    sql = store.read("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'facts_au'")[0][0]
    assert "UPDATE OF content, tags" in sql


def test_old_fts_trigger_is_narrowed_on_open(tmp_path):
    db = tmp_path / "old.db"
    s = MemoryStore(str(db), hrr_dim=DIM)
    s._write("DROP TRIGGER facts_au")
    s._write("CREATE TRIGGER facts_au AFTER UPDATE ON facts BEGIN "
             "INSERT INTO facts_fts(facts_fts, rowid, content, tags) VALUES ('delete', old.fact_id, old.content, old.tags); "
             "INSERT INTO facts_fts(rowid, content, tags) VALUES (new.fact_id, new.content, new.tags); END")
    s.close()
    s = MemoryStore(str(db), hrr_dim=DIM)
    assert "UPDATE OF" in s.read("SELECT sql FROM sqlite_master WHERE name = 'facts_au'")[0][0]
    s.close()


def test_writes_drop_the_category_bank_instead_of_rebuilding_it(store):
    store._write("INSERT INTO memory_banks (bank_name, vector, dim) VALUES ('cat:general', x'00', 1)")
    store.add_fact("Alice Smith lives in Paris")
    assert store.read("SELECT COUNT(*) FROM memory_banks")[0][0] == 0


def test_unreadable_vector_is_skipped_not_fatal(store):
    good = store.add_fact("Alice Smith lives in Paris")
    bad = store.add_fact("Alice Smith lives in Rome")
    store._write("UPDATE facts SET hrr_vector = x'0102' WHERE fact_id = ?", (bad,))
    r = _retriever(store)
    assert {x["fact_id"] for x in r.search("Alice lives", min_trust=0)} == {good, bad}
    assert good in {x["fact_id"] for x in r.related("Alice Smith")}


def test_hyphenated_identifiers_are_searchable(store):
    fact = store.add_fact("Backups run on pve-01 every night")
    assert [r["fact_id"] for r in _retriever(store).search("status of pve-01", min_trust=0)] == [fact]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_database_is_owner_only(tmp_path):
    s = MemoryStore(str(tmp_path / "p.db"), hrr_dim=DIM)
    s.add_fact("Alice Smith lives in Paris")
    s.close()
    MemoryStore(str(tmp_path / "p.db"), hrr_dim=DIM).close()
    assert (tmp_path / "p.db").stat().st_mode & 0o077 == 0


# --- provider contract ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("context", ["subagent", "cron", "flush"])
def test_non_primary_contexts_read_but_never_write(tmp_path, context):
    writer = _provider(tmp_path)
    fact = _call(writer, action="add", content="Alice Smith lives in Paris")["fact_id"]
    writer.shutdown()
    reader = _provider(tmp_path, agent_context=context, config={"mirror_memory_writes": True, "auto_extract": True})
    assert "error" in _call(reader, action="add", content="Bob Brown lives in Rome")
    assert "error" in _call(reader, action="remove", fact_id=fact)
    assert "error" in json.loads(reader.handle_tool_call("fact_feedback", {"action": "helpful", "fact_id": fact}))
    assert _call(reader, action="search", query="Paris")["count"] == 1
    reader.on_memory_write("add", "user", "Prefers short answers")
    reader.on_session_end([{"role": "user", "content": "I prefer tea over coffee in the morning"}])
    assert [f["content"] for f in reader._store.list_facts()] == ["Alice Smith lives in Paris"]
    assert reader._store.read("SELECT retrieval_count FROM facts")[0][0] == 0
    reader.shutdown()


def test_initialize_uses_the_given_profile_home(tmp_path):
    home = tmp_path / "profile-b"
    home.mkdir()
    provider = HolographicMemoryProvider(config={"hrr_dim": DIM})
    provider.initialize(session_id="s", nastech_home=str(home))
    assert Path(provider._store.db_path) == home / "memory_store.db"
    first = provider._store
    provider.initialize(session_id="s2", nastech_home=str(home))  # re-init releases the previous handle
    assert first._entry is None
    provider.shutdown()


def test_mirror_follows_replace_and_remove_by_previous_content(tmp_path):
    provider = _provider(tmp_path, config={"mirror_memory_writes": "true"})
    provider.on_memory_write("add", "memory", "Deploys go through GitHub Actions")
    provider.on_memory_write("replace", "memory", "Deploys go through a manual script",
                             metadata={"previous_content": "Deploys go through GitHub Actions"})
    assert [f["content"] for f in provider._store.list_facts()] == ["Deploys go through a manual script"]
    provider.on_memory_write("remove", "memory", "", metadata={"previous_content": "Deploys go through a manual script"})
    assert provider._store.list_facts() == []
    provider.shutdown()


def test_feedback_rejects_unknown_actions(tmp_path):
    provider = _provider(tmp_path)
    fact = _call(provider, action="add", content="Alice Smith lives in Paris")["fact_id"]
    result = json.loads(provider.handle_tool_call("fact_feedback", {"action": "meh", "fact_id": fact}))
    assert "error" in result
    assert provider._store.list_facts()[0]["trust_score"] == pytest.approx(0.5)
    provider.shutdown()


def test_limit_is_clamped(tmp_path):
    provider = _provider(tmp_path)
    for i in range(3):
        _call(provider, action="add", content=f"Alice Smith fact number {i}")
    assert _call(provider, action="list", limit=-1)["count"] == 1
    provider.shutdown()


def test_setup_defaults_are_the_saved_values(tmp_path):
    provider = HolographicMemoryProvider(config={"db_path": "/data/facts.db", "auto_extract": True})
    defaults = {f["key"]: f["default"] for f in provider.get_config_schema()}
    assert defaults["db_path"] == "/data/facts.db" and defaults["auto_extract"] == "true"
    assert defaults["hrr_dim"] == "1024"


def test_backup_paths_lists_a_database_outside_the_home(tmp_path, monkeypatch):
    outside = tmp_path / "elsewhere" / "facts.db"
    assert HolographicMemoryProvider(config={"db_path": str(outside)}).backup_paths() == [str(outside.resolve())]
    assert HolographicMemoryProvider(config={}).backup_paths() == []

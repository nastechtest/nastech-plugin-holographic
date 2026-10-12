# Holographic Memory Provider

Local memory provider for [Nastech Agent](https://github.com/NastechResearch/nastech-agent): a SQLite fact store with
FTS5 search, trust scoring, entity resolution and HRR (holographic reduced representation) retrieval. Everything
stays on your machine: no API key, no network, no model calls.

**Maintained by [nastechtest](https://github.com/nastechtest)** since October 2026. This is the provider that
shipped inside Nastech core as `plugins/memory/holographic/`; Nastech Research
[handed it off](https://github.com/NastechResearch/nastech-plugin-holographic/blob/main/HANDOFF.md) when the bundled
copy was scheduled to leave core (announced for October 15, 2026). Same provider name, same config, same database:
users who have `memory.provider: holographic` keep their facts. See [CHANGELOG.md](CHANGELOG.md) for what changed.

## How it works

![Architecture: before each turn Nastech core sends the message through the relevance gate and search; writes pass a
validator and land in one SQLite transaction; recall tools read the same file](diagram/architecture.svg)

![Recall before a turn: greetings recall nothing; the message's words become keys; candidates come from FTS5, character
n-grams or a word scan; a relevance gate and a repeat window decide; up to five facts plus entity facts go into the
turn as data](diagram/recall.svg)

## Install

While your Nastech still bundles `holographic`, the bundled copy is used and this plugin stays dormant. After core
drops it, `nastech update` installs the catalog plugin named `holographic` for every profile that has it configured.
To install by hand:

```bash
nastech plugins install holographic          # from the plugin catalog
nastech config set memory.provider holographic
```

or `nastech memory setup` and pick `holographic`.

## Config

In `config.yaml` under `plugins.nastech-memory-store`:

| Key | Default | Description |
|-----|---------|-------------|
| `db_path` | `$NASTECH_HOME/memory_store.db` | SQLite database path |
| `auto_extract` | `false` | Store user sentences that state a preference or decision (English, Russian) at session end and before context compression, at trust 0.4 |
| `mirror_memory_writes` | `false` | Also store every built-in memory `add` (MEMORY.md / USER.md) as a fact |
| `default_trust` | `0.5` | Trust score of a new fact |
| `min_trust_threshold` | `0.3` | Facts below this trust are not recalled |
| `temporal_decay_half_life` | `0` | Days; `0` turns recency decay off |
| `hrr_dim` | `1024` | HRR vector dimensions |
| `hrr_weight` | `0.3` | Weight of the HRR term in search ranking |
| `prefetch_min_coverage` | `0.3` | Share of a message's informative words (rarer words weigh more) a fact must match to be recalled before the turn |
| `prefetch_repeat_turns` | `6` | A fact recalled before a turn is not injected again for this many turns of the session |
| `recall_indicator` | `true` | Report recalled facts to the core's recall indicator ("🧠 Holographic — recalled 2 memories"). On Telegram and other messaging platforms that line arrives as a separate message before the reply; `false` turns it off, recall itself is unchanged |
| `scope` | `profile` | `user`: a separate database per user (`holographic-users/<platform>-<user id>.db` next to `db_path`); in a shared chat the store follows the author of each turn, and with no user identity nothing is recalled or written. `chat`: a separate database per chat (`holographic-chats/<platform>-<chat id>.db`) — for one bot serving many unrelated groups; `thread`: per forum topic / thread (`…-t<thread id>.db`), the chat's own database outside threads. Without a chat (CLI, cron, subagents) nothing is recalled or written |

## Tools

| Tool | Description |
|------|-------------|
| `fact_store` | add, search, probe, related, reason, contradict, update, remove, list, stats, export, review, entities, alias, enrich |
| `fact_feedback` | Rate a fact helpful or unhelpful (moves its trust score) |

- **search**: keyword candidates in any word form (`Маши` finds `Маша`, `deploys` finds `deploy`, `kucingnya` finds
  `kucing`; see [Languages](#languages)), reranked by word overlap and HRR similarity, weighted by trust.
- **Before each turn** the provider recalls up to five facts: search candidates that pass a relevance gate (the fact
  must cover enough of the message's informative words; "ok, thanks" recalls nothing) plus the top facts of entities
  the message names. A fact already injected in this session is not repeated for `prefetch_repeat_turns` turns.
  Facts are framed as data (`#id [trust] text`), one line each.
- **add** takes `entities` (who the fact is about; needed for one-word names such as a first name, so later facts
  mentioning it in any form link to it), `keywords` (see [Synonyms](#synonyms-and-other-roots-keywords)) and
  `supersedes` (the id of the fact it replaces). Its answer lists
  `similar` facts and `possible_conflicts` (same subject, a different claim); the agent decides. Text that is not a
  fact (JSON, tool or chat-role markup, instruction-like phrasing, invisible characters, long blobs, >1000
  characters) is refused with the reason.
- **stats** (what is stored and how much of it was ever recalled), **export** (Markdown view), **review**
  (clean-up candidates: low trust, never recalled for 60 days, near-duplicates), **enrich** (facts without keywords,
  most recalled first, to give keywords with `update`).
- **entities** lists names with their aliases and fact counts; **alias** gives an entity a second name (or merges
  two entities). Spelling variants are one entity (`Nastech Agent` = `nastech-agent`), and `X aka Y` makes `Y` an alias.
- **probe**: every fact linked to a named entity, from the entity index (name or alias, any letter case).
  **reason**: facts linked to *all* of several entities; when a name is not a known entity, keyword search that still
  requires every name. Entities are linked when a fact is stored: capitalized names of two to five words in any cased
  script (`Alice Smith`, `Мария Иванова`, `Jean-Luc Picard`), short quoted terms (`"Phoenix"`, `«Сбер Бизнес»`)
  and both sides of `X aka Y`; flags, `key=value`, paths, URLs and IPs are not names. A one-word name is not linked;
  probing it falls back to keyword search.
- **related**: facts structurally tied to an entity in their HRR vector, above a noise floor.
- Every recall action honours `min_trust` (default `min_trust_threshold`).
- **contradict**: candidate conflicts, i.e. pairs about the same subject that make a similar but not identical
  claim ("lives in Paris" / "lives in London"). It suggests; it never edits.

## Languages

![How a word is matched across languages: words, language, function words, keys, match, with examples that match and
examples that do not](diagram/languages.svg)

A word matches another when they share its plain form or its stem in the language of the text:

- **Stems**: Snowball for English, Russian, Spanish, French, Portuguese, Indonesian, German and Hindi, with two plural
  rules it lacks (Portuguese `ação` / `ações`, Spanish `lápiz` / `lápices`); for Arabic, Larkey's light stemmer (the one
  Lucene ships) next to Snowball's, plus the word without a glued conjunction or preposition before an alef (`لإيفا`,
  "for Eva"; `ولإيفا`, "and for Eva"); for Bengali, a light suffix stripper (Snowball has none) that leaves at least
  three letters, so `মাটি` (soil) is not `মা` (mother).
- **Which language**: the script decides for Cyrillic, Arabic, Devanagari and Bengali. Latin text is English, Spanish,
  French, Portuguese, Indonesian or German by the function words it uses ("the", "el", "le", "o", "yang", "der"); text
  with none of them is stemmed in all six. A word is dropped as a function word only when the other words show that
  language, or when it is the only sign of it in a sentence of four words or more: "son" alone, or in "where does my
  son study", is a search word; in "il boit son café" it is French "his". With no other evidence the English list
  applies, as it did to all Latin text before 0.5.0.
- **Folding**: Latin accents and ligatures (`esta` finds `está`, `coeur` finds `cœur`), Arabic vowel marks and letter
  variants, the Indic nukta, and native digits (`2027` finds `২০২৭`, `HR24` finds `HR২৪`). Folding also merges what
  only an accent tells apart (Spanish `año` / `ano`), as the FTS5 index already did.
- **Hyphens**: French elision and inversion (`d'Eva`, `prend-il`, `est-ce`) and Portuguese enclisis (`chama-se`) are
  taken apart in text of that language, reduplication (`anak-anak`) in any; a hyphen between scripts splits
  (`hr-ovh-এর`). A letters-only hyphenated token is an identifier in English text or text of no determined language
  (`hr-ovh`, `carry-on`): a message naming it is about that one, and a fact about `hr-other` is not recalled for it.
  In Spanish, French, Portuguese, Indonesian and German text it is a compound word (`week-end`, `sexta-feira`,
  `IT-Treffen`), so a host name in a question in those languages is matched like any word, not enforced.
- **German compounds**: a word matches the end of a compound in German text (`Katze` finds `Familienkatze`, `Laptop`
  finds `Arbeitslaptop`). A compound in the question is not taken apart (`Erdnussallergie` does not find `allergisch
  gegen Erdnüsse`).
- **Names** in Latin script are matched as written (and with an English `'s`): `Jean` is not mentioned by `jeans`.
  Names in other scripts carry their inflections (`Маши` mentions `Маша`, `لإيفا` mentions `إيفا`).
- **Chinese, Japanese, Korean, Thai** (no spaces between words): character n-grams, bigrams for Chinese, Japanese and
  Korean, trigrams for Thai. Grammar weighs less: Chinese function characters (的 是 了), Japanese particles (は が を
  に の), Thai function words (ของ ที่ และ); a message that is only a filler (はい, 네, โอเค) recalls nothing. Chinese
  characters (also in Japanese) count one by one as well; kana, Hangul and Thai letters, which are sounds, do not.
- **Arabic, Indic scripts, Indonesian and German** are also matched word by word against the text of the facts (so are
  Latin words of a question of undetermined language, when the store holds Indonesian or German facts): the SQLite FTS5
  index splits Indic words at their vowel signs and cannot see past Arabic and Indonesian prefixes or into German
  compounds. The rows are read on every such search; a fact is analysed once and again only when its text changes.
  Measured at 5,000 facts: 0.03–0.07 s per search and about the same for the relevance gate; the first such search
  after start analyses every fact, about 1 s at 5,000 facts and 4–6 s at 20,000 facts (later searches 0.2–0.4 s, plus
  0.15–0.2 s for the gate). A store without such facts never scans: an English store answers in about 0.02 s at 5,000
  facts, as before.

Measured on stores of 20 everyday facts per language (people, school, work, servers, plans). "Found" = the right
fact among the first five search results; "recalled" = injected before the turn; "quiet" = chit-chat, greetings and
off-topic questions recall nothing. No keywords in these stores.

| | Main set (20 questions): found / recalled | Unseen set (8 questions, 15 unrelated facts added): found / recalled | Quiet (all sets) |
|---|---|---|---|
| English | 100 % / 100 % | 100 % / 100 % | 14 / 14 |
| Russian | 100 % / 100 % | 88 % / 88 % | 14 / 14 |
| Chinese | 100 % / 100 % | 100 % / 100 % | 14 / 14 |
| Spanish | 100 % / 100 % | 100 % / 100 % | 10 / 10 |
| Hindi | 100 % / 100 % | 100 % / 100 % | 9 / 10 |
| Arabic | 100 % / 100 % | 100 % / 100 % | 10 / 10 |
| French | 100 % / 100 % | 100 % / 100 % | 10 / 10 |
| Bengali | 100 % / 100 % | 100 % / 100 % | 10 / 10 |
| Portuguese | 100 % / 100 % | 100 % / 100 % | 10 / 10 |
| Indonesian | 100 % / 100 % | 100 % / 100 % | 10 / 10 |
| Japanese | 95 % / 95 % | 100 % / 100 % | 10 / 10 |
| Korean | 100 % / 100 % | 100 % / 100 % | 10 / 10 |
| Thai | 100 % / 95 % | 100 % / 100 % | 10 / 10 |
| German | 100 % / 100 % | 75 % / 75 % | 10 / 10 |

The right fact is first in 95–100 % of main-set questions and 62–100 % of unseen ones. Recall before the turn is not
only the right fact: about one other fact of the store comes with it per question (0.8–1.2 on the main sets), and in
the unseen sets 0–2 of the 15 unrelated facts per language. English, Russian and Chinese also have a second set (10
questions): found 90 / 90 / 100 %, recalled 80 / 90 / 90 %.

How the numbers were obtained, honestly:

- **Main sets are tuned on.** English, Russian and Chinese: the fixes in 0.4.2 were made against them. Spanish, Hindi,
  Arabic, French, Bengali, Portuguese, Indonesian: first run on 0.4.2 (found 85–100 %, recalled 50–100 %, Arabic 50 %,
  French 75 %, Spanish 85 %, chit-chat recalled facts in four languages), fixed in 0.5.0. Japanese, Korean, Thai,
  German: first run on 0.5.0 (found 95–100 %, recalled 95–100 %, but chit-chat recalled facts in Korean 1/5, German
  1/5 and Thai 4/5), fixed in 0.6.0. The fixes are per language and per script, not per question.
- **Unseen sets were written before those fixes were seen to work and run once after them.** English, Russian,
  Chinese: after the run, whole-message greetings were made to recall nothing ("good morning" recalled facts about
  mornings), and the quiet column includes that fix. Spanish to Indonesian: first run before the review fixes below,
  with the same found and recalled numbers. Japanese, Korean, Thai, German: run once; nothing changed after.
- **An independent review (Codex CLI) of 0.4.2 and 0.5.0** found regressions the sets did not: a lone "son" dropped as
  a Spanish function word, `hr-ovh` no longer enforced, `ação` / `ações` apart, "with" / "without" sugar no longer a
  conflict, `Jean` mentioned by `jeans`, and others. Each is fixed with a test, and every set was run again. One of
  those fixes made French `est-ce` a required identifier, which the unseen set showed (French recalled 88 %); the
  remedy (compound words in Romance-language text, a hyphenated word votes for a language with its first part) was
  made after seeing it, so that unseen-set number is no longer blind.
- **The second set was written after the main set of 0.4.2**, first run without changes (Russian recall 90 %, Chinese
  70 %), and two fixes followed. Its numbers published with 0.4.2 (recalled 90 / 100 / 100 %) were partly luck: facts
  of a named person saved in the same second came back in arbitrary order. They now come back newest first, every
  time, and the numbers above are what that gives.
- **Not measured**: other languages (exact words, no stems, no function-word list).

### Synonyms and other roots: keywords

Words are matched, not meanings: "relocating" does not find "move", 宠物 (pet) does not find 猫 (cat). The plugin calls
no model, but the agent that stores a fact is one: `fact_store add` takes `keywords`, 3–8 words someone might ask
with that the fact does not contain (synonyms, other roots, broader terms, the same in the user's other languages).
They are kept with the fact's tags, searched like its text, never shown in the recalled block, and old versions index
them too. This is document expansion, as doc2query does for lexical search: the meaning is added once, when the fact
is written, and recall stays local and deterministic. `enrich` lists the facts that have none, so the agent or a
nightly job can add them with `update`; an update with keywords alone keeps the fact's age (`updated_at`), so old
facts do not turn into recent ones. [nastech-dreaming](https://github.com/nastechtest/nastech-dreaming) does this
nightly (`keywords.enabled`).

Measured on a third set per language: 8 questions that share no word with their fact except a name ("Where are we
relocating?", "What breed is our pet?", "Can my office computer handle AI models?"), written after the keywords. The
keywords came from a separate agent that saw only the facts and the description of `keywords`, not the questions; a
first batch was thrown away because the description's example then was the store's own "we move to Chiang Mai".

| | Without keywords: found / recalled | With keywords: found / recalled |
|---|---|---|
| English | 38 % / 38 % | 100 % / 100 % |
| Russian | 25 % / 25 % | 75 % / 62 % |
| Chinese | 38 % / 12 % | 88 % / 75 % |
| Spanish | 38 % / 25 % | 100 % / 100 % |
| French | 38 % / 38 % | 100 % / 100 % |
| Portuguese | 38 % / 38 % | 75 % / 75 % |
| Indonesian | 38 % / 25 % | 88 % / 88 % |
| Arabic | 38 % / 38 % | 75 % / 75 % |
| Hindi | 38 % / 38 % | 88 % / 75 % |
| Bengali | 38 % / 38 % | 75 % / 75 % |
| Japanese | 50 % / 25 % | 88 % / 75 % |
| Korean | 50 % / 12 % | 88 % / 62 % |
| Thai | 38 % / 25 % | 75 % / 62 % |
| German | 25 % / 38 % | 62 % / 75 % |
| **All 112** | **42 / 33** | **94 / 88** |

Without keywords, what is found comes through the name in three of the eight questions (Eva, Anton). With them, the
main sets lose nothing (280 / 280 found and recalled), one more extra fact comes per ten questions, and one chit-chat
line of 70 recalls a fact ("Apa ibu kota Australia?" shares "kota", city, with a keyword "ganti kota"). The misses
left are words the keywords did not include ("перебираемся" against "переселение", "morar" against "moradia"):
keywords are as good as the agent that writes them, and facts stored without them (older ones, `auto_extract`,
`mirror_memory_writes`) are found by their words only until `enrich` gives them some.

What it does not do: find a fact through a synonym or another root that no keyword names ("бэкапы" for "резервные
копии", Spanish `nada` for `natación`, 十月 for 10月). It can surface a fact that shares a word in another sense:
"nightly backups" → "night market"; Hindi `बनाना` is both "to cook" and "to make a plan"; Portuguese `dirigir` is "to
drive" and "to run a company". Stems are search keys, not lemmas: an irregular form can miss (`ponies` / `pony`). A
negation counts for the whole clause up to a "but" ("likes cats but not dogs" is detail, not a conflict), not for a
single word. `auto_extract` and the refusal of instruction-like text recognise English and Russian phrasing only. The
main sets of all fourteen languages run in CI (`tests/test_holographic_languages.py`).

## Data and privacy

- Facts live in one SQLite file (`db_path`), in the Nastech home of the profile, readable by its owner only (POSIX
  `0600`). Nothing is sent anywhere. A `db_path` outside the Nastech home is listed for `nastech backup`.
- Subagents, cron jobs and flush contexts read the store but never write to it (facts, feedback, counters,
  extraction).
- With `auto_extract` on, user messages matching the preference/decision patterns are stored at session end.
- `retrieval_count` counts how often a fact was handed to the model; nothing else is tracked.
- Dependencies: `numpy` (HRR algebra) and `snowballstemmer` (word forms; pure Python). Without numpy the provider
  works with keyword search only and says so in its prompt block; without snowballstemmer words compare exactly.

## Database and downgrades

Schema changes are additive: 0.4.0 adds a `meta` table (schema version, HRR dimension), an `entity_aliases` table and
an `entities.name_key` column; nothing older is dropped or rewritten, and the new column is nullable, so earlier
versions (including the copy bundled in Nastech core) keep reading and writing the file. When this version opens a
file an older one wrote to, it reconciles: missing keys are filled in, entities with the same key are merged (links,
aliases and names kept), aliases of entities the older version deleted are dropped, and vectors whose size does not
match `hrr_dim` are re-encoded. Only a writable agent does this; subagents, cron and flush contexts never migrate.

What is tested: this release, then 0.1.1, then this release on one database in CI (adds, edits, removals, aliases, a
keyless duplicate entity written by 0.1.1), plus the reconciliation cases above. What a downgrade does not keep:
aliases made after 0.4.0 are visible to 0.1.1 only through the legacy `entities.aliases` column; and with
`scope: user`, `chat` or `thread`, an older version knows nothing of per-user or per-chat files (0.6.x and older do not
know `chat` / `thread` at all and fall back to `profile`), so it reads and writes the shared `db_path` again. Those
files stay untouched (no leak), but new facts would land in the shared store.

## Development

Tests import Nastech core from a checkout, as an installed plugin does:

```bash
NASTECH_AGENT_REPO=~/.nastech/nastech-agent PYTHONPATH=~/.nastech/nastech-agent python -m pytest -q
```

CI runs the suite on Linux, macOS and Windows against nastech-agent `main`, plus `nastech plugins validate`, the check
the plugin catalog runs at admission. Maintenance notes are in [HANDOFF.md](HANDOFF.md).

Original plugin by dusterbloom (nastech-agent PR #2351); its history is in nastech-agent
(`git log -- plugins/memory/holographic`). MIT licence.

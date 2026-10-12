# Maintenance notes — nastech-plugin-holographic

**Status: maintained by [nastechtest](https://github.com/nastechtest)** (since 2026-10-10). This repository
started as Nastech Research's standalone copy of the `holographic` memory provider bundled in
`NastechResearch/nastech-agent` under `plugins/memory/holographic/`, published so someone could take it over
([original handoff note](https://github.com/NastechResearch/nastech-plugin-holographic/blob/main/HANDOFF.md),
[takeover request](https://github.com/NastechResearch/nastech-plugin-holographic/issues/1)).

## Catalog and migration

The plugin is submitted to the Nastech plugin catalog as `plugin-catalog/holographic.yaml` under the name
`holographic`. That exact name matters: once core drops its bundled copy, `nastech update` (and agent start) install
the catalog plugin of the same name for users with `memory.provider: holographic`
(`nastech_cli/memory_provider_migration.py`, `_LEFT_CORE`), keeping their config and data.

Releases are tags `vX.Y.Z` matching `version` in `plugin.yaml` and `pyproject.toml`; the catalog entry pins the
tag's commit by `sha:` and `version:`. A new release reaches users only through a SHA-bump PR to nastech-agent.

## Differences from the in-tree copy

- Self-imports are relative so the package loads from `~/.nastech/plugins/holographic/` under the loader's synthetic
  namespace.
- `pyproject.toml` is the dependency authority (no `tools.lazy_deps` calls: they pinned an old version in the
  lazy-deps registry and downgraded newer installs, nastech-agent#86992).
- Behaviour changes since 0.2.0 are listed in [CHANGELOG.md](CHANGELOG.md).

While a Nastech install still carries the bundled `plugins/memory/holographic`, that copy wins on name and this plugin
is dormant; it takes over once core drops the bundled directory.

## Tests

`tests/` holds the in-tree tests (`tests/plugins/memory/test_holographic*.py` in nastech-agent) with the import path
switched to the package `tests/conftest.py` loads from this repo, plus `test_holographic_v020.py`. Run locally with
`NASTECH_AGENT_REPO=~/.nastech/nastech-agent PYTHONPATH=~/.nastech/nastech-agent python -m pytest -q`. The checkout
directory name must be a valid Python identifier (CI uses `plugin`): pytest imports the repository root as a package.
CI runs the suite on Linux, macOS and Windows against nastech-agent `main`, and `nastech plugins validate
--install-deps`, the catalog's admission check.

## Languages

`text.py` holds everything per language: function words (`STOP`), stemmer choice (`_STEMMERS`, `_DEFAULT_LANG`,
`_SCRIPT_LANGS`, `LATIN`), greetings, negators, claim words, folding (`_FOLD`); for the n-gram scripts the function
characters (`CJK_FUNCTION`, Japanese particles included), Thai function words (`_TH_FUNCTION`) and filler runs
(`FILLER_RUNS`). Synonyms come from the agent's `keywords` (stored after a `keywords:` line in `tags`, see
`_split_tags` / `_join_tags` in `__init__.py`); the keyword benchmark generated them with a separate agent that never
saw the questions, and the example in the `keywords` description must never be a benchmark fact (one batch was
discarded for that). To add a language: its function words (they
also decide which Latin language a text is), its stemmer, greetings and negators; then a main set and an unseen set in
`tests/test_holographic_languages.py` style, measured before the change. Arabic, Indic scripts and Indonesian go
through the word scan in `retrieval.py` (`_texts`, `_scan_candidates`), not FTS5; keep that in mind for any script
whose words FTS5 splits or whose inflection is a prefix. The README "Languages" section states how every number was
obtained; keep it that way.

## Open work

- Unsegmented scripts use a substring scan of character n-grams; an FTS5 trigram index would scale further but needs
  a sync story for older versions writing the file (no triggers: they would fail on SQLite builds without trigram).
  Not needed at this store's size; revisit if stores grow past ~50k facts.
- Paraphrase recall (no shared words) needs embeddings; deliberately out of scope (no network, no model).
- The word scan reads every row on each scan; past ~20k facts a persisted key index (with a reconciliation story for
  older versions writing the file) would be the next step. Host names in Spanish/French/Portuguese/Indonesian/German
  questions are not enforced as identifiers (compound words win there). German compounds in the question are not
  split (`Erdnussallergie`); keywords cover that today.
- The nightly dreaming skill could run `enrich` and fill keywords for old facts; not wired yet.
- Upstream PRs to the bundled copy that were never merged and are worth reviewing here: nastech-agent#133389
  (retrieval contract tests), #123599 (setup offers saved config).

Original authors are preserved in nastech-agent's history: `git log -- plugins/memory/holographic`.

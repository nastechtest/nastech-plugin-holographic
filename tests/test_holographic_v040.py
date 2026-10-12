"""0.4.0: entity identity keys and aliases, the recorded HRR dimension, unsegmented scripts, per-user stores."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("numpy")

from nastech_plugin_holographic import HolographicMemoryProvider
from nastech_plugin_holographic.retrieval import FactRetriever
from nastech_plugin_holographic.store import MemoryStore, entity_key

DIM = 512


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(str(tmp_path / "facts.db"), hrr_dim=DIM)
    yield s
    s.close()


def _r(store):
    return FactRetriever(store, hrr_dim=DIM)


def _call(provider, **args):
    return json.loads(provider.handle_tool_call("fact_store", args))


# --- entity identity --------------------------------------------------------------------------------------------


def test_entity_key_unifies_spelling_not_meaning():
    assert entity_key("Nastech Agent") == entity_key("nastech-agent") == entity_key("NASTECH  AGENT") == entity_key("Nastech_Agent")
    assert entity_key("co-op") != entity_key("coop")
    assert entity_key("Мария Иванова") == entity_key("МАРИЯ ИВАНОВА")


def test_spelling_variants_are_one_entity(store):
    a = store.add_fact("Nastech Agent runs on the server")
    b = store.add_fact('The repo "nastech-agent" moved to a new org')
    assert store.read("SELECT COUNT(*) FROM entities")[0][0] == 1
    assert {f["fact_id"] for f in _r(store).probe("NASTECH-AGENT")} == {a, b}


def test_aka_makes_an_alias_not_a_second_entity(store):
    fact = store.add_fact("Маша aka Мария Петровна ходит в бассейн")
    assert [f["fact_id"] for f in _r(store).probe("Мария Петровна")] == [fact]
    entities = store.list_entities()
    assert [(e["name"], e["aliases"]) for e in entities] == [("Маша", ["Мария Петровна"])]
    later = store.add_fact("Мария Петровна любит плавать")  # the alias links later facts too
    assert {f["fact_id"] for f in _r(store).probe("Маша")} == {fact, later}


def test_alias_action_merges_two_entities(tmp_path):
    provider = HolographicMemoryProvider(config={"db_path": str(tmp_path / "m.db"), "hrr_dim": DIM})
    provider.initialize(session_id="s")
    a = _call(provider, action="add", content="Alice Smith lives in Paris")["fact_id"]
    b = _call(provider, action="add", content="Ally Smith plays chess")["fact_id"]
    merged = _call(provider, action="alias", entity="Alice Smith", content="Ally Smith")
    assert merged["merged"]
    assert {r["fact_id"] for r in _call(provider, action="probe", entity="Ally Smith")["results"]} == {a, b}
    assert [e["name"] for e in _call(provider, action="entities")["entities"]] == ["Alice Smith"]
    provider.shutdown()


def test_entities_written_by_an_older_version_are_reconciled_on_open(tmp_path):
    db = tmp_path / "facts.db"
    s = MemoryStore(str(db), hrr_dim=DIM)
    first = s.add_fact("Nastech Agent runs on the server")
    # what an older version leaves behind: a second entity of the same name, no key, linked to its own fact
    second = s.add_fact("deploys are weekly")
    s._write("INSERT INTO entities (name) VALUES ('nastech-agent')")
    s._write("INSERT INTO fact_entities (fact_id, entity_id) VALUES (?, last_insert_rowid())", (second,))
    s.close()
    s = MemoryStore(str(db), hrr_dim=DIM)
    assert s.read("SELECT COUNT(*) FROM entities")[0][0] == 1
    assert {f["fact_id"] for f in _r(s).probe("Nastech Agent")} == {first, second}
    s.close()


# --- HRR dimension ----------------------------------------------------------------------------------------------


def test_changed_hrr_dim_reencodes_instead_of_failing(tmp_path):
    db = tmp_path / "facts.db"
    s = MemoryStore(str(db), hrr_dim=256)
    fact = s.add_fact("Alice Smith lives in Paris")
    s.close()
    s = MemoryStore(str(db), hrr_dim=DIM)
    assert s.read("SELECT value FROM meta WHERE key = 'hrr_dim'")[0][0] == str(DIM)
    assert [f["fact_id"] for f in _r(s).related("Alice Smith")] == [fact]
    s.close()


# --- unsegmented scripts ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("fact,query", [
    ("ร้านกาแฟเปิดตอนเจ็ดโมงเช้า", "ร้านกาแฟเปิดกี่โมง"),         # Thai: when does the coffee shop open
    ("我们每周五下午开会", "开会是什么时候"),                         # Chinese: when is the meeting
    ("学校は八時に始まります", "学校は何時から"),                       # Japanese: when does school start
    ("학교에서 수영을 배워요", "학교 수영"),                            # Korean: particle attached to the noun
])
def test_unsegmented_scripts_are_searchable(store, fact, query):
    store.add_fact("Alice Smith lives in Paris")
    fact_id = store.add_fact(fact)
    assert [f["fact_id"] for f in _r(store).search(query, min_trust=0)][:1] == [fact_id]


def test_prefetch_recalls_thai(tmp_path):
    provider = HolographicMemoryProvider(config={"db_path": str(tmp_path / "m.db"), "hrr_dim": DIM})
    provider.initialize(session_id="s")
    fact = _call(provider, action="add", content="ร้านกาแฟเปิดตอนเจ็ดโมงเช้า")["fact_id"]
    _call(provider, action="add", content="Alice Smith lives in Paris")
    assert f"#{fact} " in provider.prefetch("ร้านกาแฟเปิดกี่โมง")
    provider.shutdown()


# --- per-user stores --------------------------------------------------------------------------------------------


def _user_provider(tmp_path, **kwargs):
    provider = HolographicMemoryProvider(config={"db_path": str(tmp_path / "memory_store.db"), "hrr_dim": DIM, "scope": "user"})
    provider.initialize(session_id="s", nastech_home=str(tmp_path), platform="telegram", **kwargs)
    return provider


def test_user_scope_keeps_each_person_apart(tmp_path):
    provider = _user_provider(tmp_path, user_id="111")
    _call(provider, action="add", content="Alice Smith is allergic to nuts")
    provider.on_turn_start(2, "hi", author_id="222", author_name="Bob")
    assert _call(provider, action="search", query="allergic nuts")["count"] == 0
    assert provider.prefetch("is Alice Smith allergic to nuts?") == ""
    _call(provider, action="add", content="Bob Brown prefers tea")
    provider.on_turn_start(3, "hi", author_id="111")
    assert _call(provider, action="search", query="allergic nuts")["count"] == 1
    assert _call(provider, action="search", query="tea")["count"] == 0
    provider.shutdown()
    assert sorted(p.name for p in (tmp_path / "holographic-users").iterdir() if p.suffix == ".db") == [
        "telegram-111.db", "telegram-222.db"]
    assert not (tmp_path / "memory_store.db").exists()  # nothing landed in the shared store


def test_user_scope_without_identity_fails_closed(tmp_path):
    provider = _user_provider(tmp_path)
    assert "error" in _call(provider, action="add", content="Alice Smith lives in Paris")
    assert provider.prefetch("Alice Smith") == ""
    assert "no user identity" in provider.system_prompt_block()
    provider.on_turn_start(1, "hi", author_id="333", author_is_bot=True)  # a bot is not a user
    assert "error" in _call(provider, action="list")
    provider.shutdown()


def test_user_ids_are_sanitised_into_file_names(tmp_path):
    provider = _user_provider(tmp_path, user_id="../../etc/passwd")
    _call(provider, action="add", content="Alice Smith lives in Paris")
    provider.shutdown()
    files = list((tmp_path / "holographic-users").iterdir())
    assert all(f.parent == tmp_path / "holographic-users" for f in files)
    assert any(f.name.startswith("telegram-.._.._etc_passwd-") for f in files)

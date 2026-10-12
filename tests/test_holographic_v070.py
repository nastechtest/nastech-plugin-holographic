"""0.7.0: memory scoped per chat (scope: chat) or per forum topic / thread (scope: thread)."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("numpy")

from nastech_plugin_holographic import HolographicMemoryProvider

DIM = 512


def _call(provider, **args):
    return json.loads(provider.handle_tool_call("fact_store", args))


def _chat(tmp_path, scope="chat", **kwargs):
    config = {"db_path": str(tmp_path / "memory_store.db"), "hrr_dim": DIM, "scope": scope}
    config.update(kwargs.pop("config", {}))
    provider = HolographicMemoryProvider(config=config)
    provider.initialize(session_id="s", nastech_home=str(tmp_path), platform="telegram", **kwargs)
    return provider


def _files(tmp_path):
    folder = tmp_path / "holographic-chats"
    return sorted(p.name for p in folder.iterdir() if p.suffix == ".db") if folder.is_dir() else []


def test_chat_scope_keeps_each_chat_apart(tmp_path):
    a = _chat(tmp_path, chat_id="-1001", user_id="111")
    _call(a, action="add", content="Group A meets every Friday at the Hilton")
    b = _chat(tmp_path, chat_id="-1002", user_id="111")  # the same person, another group
    assert _call(b, action="search", query="Friday Hilton")["count"] == 0
    assert b.prefetch("when does the group meet at the Hilton on Friday?") == ""
    _call(b, action="add", content="Group B meets on Mondays online")
    assert _call(a, action="search", query="Mondays online")["count"] == 0
    assert _call(a, action="search", query="Friday Hilton")["count"] == 1
    assert "per chat" in a.system_prompt_block()
    a.shutdown()
    b.shutdown()
    assert _files(tmp_path) == ["telegram--1001.db", "telegram--1002.db"]
    assert not (tmp_path / "memory_store.db").exists()  # nothing landed in the shared store


def test_chat_scope_ignores_who_is_talking(tmp_path):
    """Unlike user scope, the store does not follow the author: the chat's facts belong to everybody in it."""
    provider = _chat(tmp_path, chat_id="-1001")
    provider.on_turn_start(1, "hi", author_id="111", author_name="Alice")
    _call(provider, action="add", content="The group rules forbid advertising")
    provider.on_turn_start(2, "hi", author_id="222", author_name="Bob")
    assert _call(provider, action="search", query="rules advertising")["count"] == 1
    provider.on_turn_start(3, "hi", author_id="333", author_is_bot=True)
    assert _call(provider, action="search", query="rules advertising")["count"] == 1
    provider.shutdown()


@pytest.mark.parametrize("scope", ["chat", "thread"])
def test_without_a_chat_nothing_is_recalled_or_written(tmp_path, scope):
    provider = _chat(tmp_path, scope=scope, user_id="111")  # CLI, cron, subagents: no chat
    assert "error" in _call(provider, action="add", content="Alice Smith lives in Paris")
    assert "no chat" in _call(provider, action="list")["error"]
    assert provider.prefetch("where does Alice Smith live?") == ""
    assert "no chat" in provider.system_prompt_block()
    provider.shutdown()
    assert _files(tmp_path) == [] and not (tmp_path / "memory_store.db").exists()


def test_thread_scope_keeps_each_topic_apart(tmp_path):
    support = _chat(tmp_path, scope="thread", chat_id="-1001", thread_id="7")
    _call(support, action="add", content="Refunds are processed within five days")
    sales = _chat(tmp_path, scope="thread", chat_id="-1001", thread_id="9")
    assert _call(sales, action="search", query="refunds processed")["count"] == 0
    general = _chat(tmp_path, scope="thread", chat_id="-1001")  # no thread: the chat's own store
    assert _call(general, action="search", query="refunds processed")["count"] == 0
    _call(general, action="add", content="The chat language is English")
    for p in (support, sales, general):
        p.shutdown()
    assert _files(tmp_path) == ["telegram--1001-t7.db", "telegram--1001-t9.db", "telegram--1001.db"]


def test_chat_scope_shares_one_store_across_threads(tmp_path):
    one = _chat(tmp_path, chat_id="-1001", thread_id="7")
    _call(one, action="add", content="Refunds are processed within five days")
    other = _chat(tmp_path, chat_id="-1001", thread_id="9")
    assert _call(other, action="search", query="refunds processed")["count"] == 1
    one.shutdown()
    other.shutdown()
    assert _files(tmp_path) == ["telegram--1001.db"]


def test_auto_extract_files_facts_under_the_chat(tmp_path):
    provider = _chat(tmp_path, chat_id="-1001", config={"auto_extract": "true"})
    provider.sync_turn("I prefer green tea in the morning.", "ok", turn_author={"id": "111", "name": "A", "is_bot": False})
    provider.on_session_end([{"role": "user", "content": "We decided to meet on Fridays."}])
    assert _call(provider, action="search", query="green tea")["count"] == 1
    provider.shutdown()
    assert not (tmp_path / "memory_store.db").exists()  # extraction never wrote the shared store


def test_chat_ids_are_sanitised_into_file_names(tmp_path):
    provider = _chat(tmp_path, chat_id="../../etc/passwd")
    _call(provider, action="add", content="Alice Smith lives in Paris")
    provider.shutdown()
    files = list((tmp_path / "holographic-chats").iterdir())
    assert all(f.parent == tmp_path / "holographic-chats" for f in files)
    assert any(f.name.startswith("telegram-.._.._etc_passwd-") for f in files)


def test_profile_and_user_scope_are_unchanged(tmp_path):
    profile = _chat(tmp_path, scope="profile", chat_id="-1001")
    _call(profile, action="add", content="Alice Smith lives in Paris")
    profile.shutdown()
    assert (tmp_path / "memory_store.db").exists() and _files(tmp_path) == []

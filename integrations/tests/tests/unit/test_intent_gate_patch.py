"""The intent gate decides whether a turn is kept, without ever losing one.

``store-user-prompt.py`` parks the prompt, then asks the gate. The verdict
rides on the pending entry to the Stop hook, which skips the QA row for a
dropped turn and files a preference under the shared ``user_context`` node set.
Whenever the gate is off, slow, or broken, the turn is stored as before.

Copy into ``integrations/tests/tests/unit/`` of a cognee-integrations checkout
that has the patch applied. Only the claude-code suite carries the patch.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

DROP = {"save": False, "choice": "command", "confidence": 0.93, "node_set": None, "source": "jev"}
KEEP = {"save": True, "choice": "spec_rule", "confidence": 0.86, "node_set": "repo", "source": "jev"}
PREFERENCE = {"save": True, "choice": "preference", "confidence": 0.9, "node_set": "user_context", "source": "jev"}


@pytest.fixture(autouse=True)
def _claude_code_only(suite):
    if suite.name != "claude-code":
        pytest.skip("the intent-gate patch targets the claude-code integration")


@pytest.fixture
def pc(suite, isolated_modules, monkeypatch):
    module = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    monkeypatch.setenv("COGNEE_SESSION_KEY", "host-abc")
    return module


# ---- pending prompt carries the verdict -----------------------------------


def test_verdict_rides_on_the_pending_prompt(pc):
    pc.remember_pending_prompt("s1", "CI 過了就 merge", turn_id="t1")
    assert pc.annotate_pending_prompt("s1", DROP, turn_id="t1", prompt="CI 過了就 merge")
    popped = pc.pop_pending_prompt("s1", turn_id="t1")
    assert popped["prompt"] == "CI 過了就 merge"
    assert popped["gate"] == DROP


def test_pop_shape_is_unchanged_without_a_verdict(pc):
    pc.remember_pending_prompt("s1", "hello there", turn_id="t1")
    assert set(pc.pop_pending_prompt("s1", turn_id="t1")) == {"prompt", "context"}


def test_late_verdict_does_not_resurrect_a_consumed_prompt(pc):
    pc.remember_pending_prompt("s1", "CI 過了就 merge", turn_id="t1")
    pc.pop_pending_prompt("s1", turn_id="t1")  # Stop outran the judge
    assert not pc.annotate_pending_prompt("s1", DROP, turn_id="t1", prompt="CI 過了就 merge")
    assert not pc._pending_file("s1").exists()


def test_late_verdict_never_lands_on_a_newer_prompt(pc):
    pc.remember_pending_prompt("s1", "以後 commit 訊息一律用英文")
    assert not pc.annotate_pending_prompt("s1", DROP, prompt="CI 過了就 merge")
    assert "gate" not in pc.pop_pending_prompt("s1")


# ---- node set on the stored entry -----------------------------------------


@pytest.fixture
def sent(pc, monkeypatch):
    """Capture what remember_entry_via_http posts, with a controllable route."""
    posted: list[dict] = []
    route = {"primary": "ds", "write": "ds", "node_set": []}
    pm = sys.modules.get("_project_memory") or __import__("_project_memory")
    monkeypatch.setattr(pm, "route", lambda dataset, session_id: dict(route))
    monkeypatch.setattr(pc, "dataset_id_for", lambda dataset: None)
    monkeypatch.setattr(pc, "_json_http_request", lambda path, payload, **k: posted.append(payload) or {})
    return posted, route


def test_own_node_set_wins_where_project_node_sets_are_active(pc, sent):
    posted, route = sent
    route["node_set"] = ["project-x"]
    entry = {"type": "qa", "question": "q", "answer": "a", "node_set": ["user_context"]}
    pc.remember_entry_via_http("ds", "s1", entry)
    assert posted[0]["entry"]["node_set"] == ["user_context"]
    assert entry["node_set"] == ["user_context"], "the caller's entry must survive for buffering"


def test_project_node_set_is_the_default(pc, sent):
    posted, route = sent
    route["node_set"] = ["project-x"]
    pc.remember_entry_via_http("ds", "s1", {"type": "qa", "question": "q", "answer": "a"})
    assert posted[0]["entry"]["node_set"] == ["project-x"]


def test_own_node_set_is_not_sent_to_a_backend_without_project_node_sets(pc, sent):
    posted, _ = sent
    pc.remember_entry_via_http(
        "ds", "s1", {"type": "qa", "question": "q", "answer": "a", "node_set": ["user_context"]}
    )
    assert "node_set" not in posted[0]["entry"]


# ---- UserPromptSubmit: park, then judge -----------------------------------


@pytest.fixture
def prompt_hook(suite, hook_module, monkeypatch, closed_port_url, tmp_path):
    monkeypatch.setenv("COGNEE_BASE_URL", closed_port_url)
    monkeypatch.setenv("COGNEE_SESSION_KEY", "host-1")
    monkeypatch.setenv("COGNEE_IDLE_DISABLED", "1")
    module = hook_module(suite, "store-user-prompt.py")
    events: list[tuple] = []
    monkeypatch.setattr(module, "hook_log", lambda event, detail=None: events.append((event, detail)))
    monkeypatch.setattr(module, "_load_session", lambda: ("s1", "ds", "", ""))
    monkeypatch.setattr(module, "server_usable", lambda *a, **k: False)
    monkeypatch.setattr(module, "notify", lambda *a, **k: None)
    counted: list[str] = []
    monkeypatch.setattr(module, "bump_save_counter", lambda session, kind, **k: counted.append(kind))
    gate = sys.modules["_intent_gate"] if "_intent_gate" in sys.modules else __import__("_intent_gate")
    module.events, module.counted, module.gate_client = events, counted, gate
    module.payload = {"cwd": str(tmp_path / "repo"), "session_id": "sess", "prompt_id": "p1"}
    return module


def _pending(module) -> dict:
    return sys.modules["_plugin_common"].pop_pending_prompt("s1")


def test_gate_off_changes_nothing(prompt_hook, monkeypatch):
    monkeypatch.delenv("COGNEE_CAPTURE_JUDGE", raising=False)
    monkeypatch.setattr(prompt_hook.gate_client, "judge", lambda *a, **k: pytest.fail("gate must not be called"))
    asyncio.run(prompt_hook._store("CI 過了就 merge", prompt_hook.payload))
    assert set(_pending(prompt_hook)) == {"prompt", "context"}
    assert prompt_hook.counted == ["prompt"]


def test_dropped_prompt_is_marked_and_not_counted(prompt_hook, monkeypatch):
    monkeypatch.setenv("COGNEE_CAPTURE_JUDGE", "true")
    seen: dict = {}

    def judge(text, **kwargs):
        seen.update(kwargs, text=text)
        return dict(DROP)

    monkeypatch.setattr(prompt_hook.gate_client, "judge", judge)
    asyncio.run(prompt_hook._store("CI 過了就 merge", prompt_hook.payload))
    pending = _pending(prompt_hook)
    assert pending["prompt"] == "CI 過了就 merge", "the prompt stays parked until Stop consumes it"
    assert pending["gate"]["save"] is False and pending["gate"]["choice"] == "command"
    assert pending["gate"]["turn"] == "sess:p1", "the verdict names the turn it was made for"
    assert prompt_hook.counted == []
    assert seen["text"] == "CI 過了就 merge" and seen["project"] == "repo" and seen["key"] == "sess:p1"


def test_kept_project_prompt_uses_the_default_route(prompt_hook, monkeypatch):
    monkeypatch.setenv("COGNEE_CAPTURE_JUDGE", "true")
    monkeypatch.setattr(prompt_hook.gate_client, "judge", lambda *a, **k: dict(KEEP))
    asyncio.run(prompt_hook._store("反之如果刪除段拉到其他操作段就以操作段為主", prompt_hook.payload))
    gate = _pending(prompt_hook)["gate"]
    assert gate["save"] is True and gate["node_set"] is None, "the project's own node set is not an override"
    assert prompt_hook.counted == ["prompt"]


def test_preference_is_routed_to_user_context(prompt_hook, monkeypatch):
    monkeypatch.setenv("COGNEE_CAPTURE_JUDGE", "true")
    monkeypatch.setattr(prompt_hook.gate_client, "judge", lambda *a, **k: dict(PREFERENCE))
    asyncio.run(prompt_hook._store("以後 commit 訊息一律用英文", prompt_hook.payload))
    assert _pending(prompt_hook)["gate"]["node_set"] == "user_context"


def test_unreachable_gate_keeps_the_prompt(prompt_hook, monkeypatch, closed_port_url):
    monkeypatch.setenv("COGNEE_CAPTURE_JUDGE", "true")
    monkeypatch.setenv("INTENT_GATE_URL", closed_port_url)
    monkeypatch.setenv("INTENT_GATE_TOKEN", "t")
    asyncio.run(prompt_hook._store("CI 過了就 merge", prompt_hook.payload))
    gate = _pending(prompt_hook)["gate"]
    assert gate["save"] is True and gate["source"] == "fallback" and gate["node_set"] is None
    assert prompt_hook.counted == ["prompt"]
    assert any(event == "intent_gate_failed" for event, _ in prompt_hook.events)


def test_a_crashing_gate_client_keeps_the_prompt(prompt_hook, monkeypatch):
    monkeypatch.setenv("COGNEE_CAPTURE_JUDGE", "true")

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(prompt_hook.gate_client, "judge", boom)
    asyncio.run(prompt_hook._store("CI 過了就 merge", prompt_hook.payload))
    assert "gate" not in _pending(prompt_hook)
    assert prompt_hook.counted == ["prompt"]


def test_previous_assistant_reply_is_sent_as_context(prompt_hook, monkeypatch, tmp_path):
    monkeypatch.setenv("COGNEE_CAPTURE_JUDGE", "true")
    transcript = tmp_path / "t.jsonl"
    rows = [
        {"type": "user", "message": {"content": "排版怎麼做"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "A 單頁式還是 B 分頁式？"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}},
    ]
    transcript.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n{partial", encoding="utf-8")
    seen: dict = {}
    monkeypatch.setattr(prompt_hook.gate_client, "judge", lambda text, **k: seen.update(k) or dict(DROP))
    asyncio.run(prompt_hook._store("B + 2 就好", {**prompt_hook.payload, "transcript_path": str(transcript)}))
    assert seen["context"] == "A 單頁式還是 B 分頁式？"


# ---- Stop: honour the verdict ---------------------------------------------


@pytest.fixture
def stop_hook(suite, hook_module, monkeypatch, closed_port_url):
    monkeypatch.setenv("COGNEE_BASE_URL", closed_port_url)
    monkeypatch.setenv("COGNEE_SESSION_KEY", "host-1")
    module = hook_module(suite, "store-to-session.py")
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    monkeypatch.setattr(module, "_load_session", lambda: ("s1", "ds", ""))
    monkeypatch.setattr(module, "server_usable", lambda *a, **k: False)
    buffered: list[dict] = []
    monkeypatch.setattr(module, "append_warmup_entry", lambda dataset, session, entry, **k: buffered.append(entry))
    monkeypatch.setattr(module, "bump_save_counter", lambda *a, **k: None)
    module.buffered = buffered
    module.pc = sys.modules["_plugin_common"]
    return module


STOP_PAYLOAD = {"last_assistant_message": "done.", "session_id": "sess", "prompt_id": "p1"}


def _stop(module, prompt: str, gate: dict | None, payload: dict = STOP_PAYLOAD) -> list[dict]:
    module.pc.remember_pending_prompt("s1", prompt)
    if gate is not None:
        module.pc.annotate_pending_prompt("s1", {"turn": "sess:p1", **gate}, prompt=prompt)
    asyncio.run(module._store_assistant_stop(dict(payload)))
    return module.buffered


def test_dropped_turn_stores_no_qa_row(stop_hook):
    assert _stop(stop_hook, "CI 過了就 merge", {"save": False, "choice": "command", "node_set": None}) == []
    assert not stop_hook.pc._pending_file("s1").exists(), "the parked prompt is still consumed"


@pytest.mark.parametrize("choice", ["question", "bug_report"])
def test_answer_bearing_drop_intents_keep_the_turn(stop_hook, choice):
    (entry,) = _stop(stop_hook, "為什麼拉取段之後就點不到了", {"save": False, "choice": choice, "node_set": None})
    assert entry["question"].startswith("為什麼") and entry["answer"] == "done." and "node_set" not in entry


def test_kept_turn_is_stored_as_before(stop_hook):
    (entry,) = _stop(stop_hook, "反之如果刪除段拉到其他操作段就以操作段為主", {"save": True, "choice": "spec_rule", "node_set": None})
    assert entry["question"].startswith("反之") and entry["answer"] == "done." and "node_set" not in entry


def test_preference_turn_carries_user_context(stop_hook):
    (entry,) = _stop(stop_hook, "以後 commit 訊息一律用英文", {"save": True, "choice": "preference", "node_set": "user_context"})
    assert entry["node_set"] == ["user_context"]


def test_turn_without_a_verdict_is_stored_as_before(stop_hook):
    (entry,) = _stop(stop_hook, "CI 過了就 merge", None)
    assert entry["question"] == "CI 過了就 merge" and "node_set" not in entry


def test_a_verdict_left_over_from_another_turn_is_ignored(stop_hook):
    """An interrupted turn leaves its prompt parked; the next Stop must not inherit its drop."""
    later_turn = {**STOP_PAYLOAD, "prompt_id": "p2"}
    (entry,) = _stop(stop_hook, "這個函式是做什麼的？", {"save": False, "choice": "question", "node_set": None}, later_turn)
    assert entry["answer"] == "done.", "stored as before the gate existed"


def test_a_stale_preference_route_is_ignored_too(stop_hook):
    later_turn = {**STOP_PAYLOAD, "prompt_id": "p2"}
    (entry,) = _stop(stop_hook, "以後 commit 訊息一律用英文", {"save": True, "choice": "preference", "node_set": "user_context"}, later_turn)
    assert "node_set" not in entry


def test_a_verdict_is_ignored_when_the_host_names_no_turn(stop_hook):
    no_turn = {"last_assistant_message": "done."}
    (entry,) = _stop(stop_hook, "CI 過了就 merge", {"save": False, "choice": "command", "node_set": None}, no_turn)
    assert entry["question"] == "CI 過了就 merge"

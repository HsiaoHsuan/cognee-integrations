"""Session distill: only chosen memories reach long-term memory, always with a node set."""

import json

import pytest


@pytest.fixture
def distill(suite, isolated_modules):
    if suite.name != "claude-code":
        pytest.skip("session distill ships with the Claude Code plugin only")
    return isolated_modules(suite, "_distill")


def _memory(text, *, type_="decision", why=None, evidence="[U1] x", confidence=0.8):
    return {
        "type": type_,
        "text": text,
        "why": why,
        "evidence": evidence,
        "scope": "project",
        "confidence": confidence,
        "replaces": None,
    }


def _transcript(path, rows):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    return path


def test_condense_keeps_what_was_said_and_drops_noise(distill, tmp_path):
    path = _transcript(
        tmp_path / "s.jsonl",
        [
            {
                "type": "user",
                "message": {"content": "以後都用 pnpm<system-reminder>x</system-reminder>"},
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "secret plan"},
                        {"type": "text", "text": "好，改成 pnpm。"},
                        {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.json"}},
                    ]
                },
            },
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "big"}]}},
            {"type": "user", "isSidechain": True, "message": {"content": "subagent"}},
            {"type": "attachment"},
        ],
    )
    assert distill.condense(path) == [
        "[U1] 以後都用 pnpm",
        "[A1] 好，改成 pnpm。\n(tool Edit: a.json)",
    ]


def test_units_count_display_width(distill):
    assert distill.units("帳號鎖定") == 4
    assert distill.units("abcd") == 2
    assert distill.too_long(_memory("字" * 71))
    assert not distill.too_long(_memory("字" * 70))


def test_node_sets_never_empty(distill):
    assert distill.node_sets(_memory("x", type_="preference"), "project-a") == [
        "user_context",
        "preference",
    ]
    assert distill.node_sets(_memory("x", type_="bug_fix"), "project-a") == ["project-a", "bug_fix"]


def test_tighten_merges_undoes_fragments_and_restores_dropped(distill, monkeypatch):
    inputs = [
        _memory("新增模組一律用 ddd 範本並綁到主宿主專案上面", evidence="e0"),
        _memory("鎖定時間放 appsettings，不放 AbpSettings", evidence="e1"),
        _memory("鎖定時間放 appsettings，各環境可各自設定", evidence="e2"),
        _memory("會被模型漏掉的一條記憶內容", evidence="e3"),
    ]

    def fake(prompt, model, schema=None):
        return {
            "memories": [
                {"sources": [0], "type": "decision", "text": "DDD 分層", "why": None},
                {"sources": [0], "type": "decision", "text": "模組綁到主宿主專案上", "why": None},
                {
                    "sources": [1, 2],
                    "type": "decision",
                    "text": "鎖定時間只放 appsettings",
                    "why": None,
                },
            ]
        }

    monkeypatch.setattr(distill, "ask_claude", fake)
    out = {tuple(m["sources"]): m for m in distill.tighten(inputs, "m")}
    assert set(out) == {(0,), (1, 2), (3,)}
    assert out[(0,)]["text"] == inputs[0]["text"]  # the fragmenting split is undone
    assert out[(1, 2)]["evidence"] == "e1 / e2"


def test_tighten_retries_long_ones_shorten_only(distill, monkeypatch):
    inputs = [_memory("短的記憶一條，內容足夠長了"), _memory("超長" * 50)]
    prompts = []

    def fake(prompt, model, schema=None):
        prompts.append(prompt)
        if len(prompts) == 1:
            return {
                "memories": [
                    {"sources": [0], "type": "decision", "text": inputs[0]["text"], "why": None},
                    {"sources": [1], "type": "decision", "text": "還是很長" * 30, "why": None},
                ]
            }
        assert "Do not split" in prompt
        return {"memories": [{"sources": [0], "type": "decision", "text": "縮短後", "why": None}]}

    monkeypatch.setattr(distill, "ask_claude", fake)
    out = distill.tighten(inputs, "m")
    assert [m["text"] for m in out] == [inputs[0]["text"], "縮短後"]
    assert out[1]["sources"] == [1]


def test_distill_drops_low_confidence(distill, monkeypatch):
    def fake(prompt, model, schema=None):
        if schema is not distill.REWRITE_SCHEMA:
            return {
                "memories": [
                    _memory("留下來的一條決定內容"),
                    _memory("太沒把握的一條", confidence=0.5),
                ],
                "rejected": [],
            }
        return {
            "memories": [
                {"sources": [0], "type": "decision", "text": "留下來的一條決定內容", "why": None}
            ]
        }

    monkeypatch.setattr(distill, "ask_claude", fake)
    result = distill.distill(["[U1] hi"], "proj", "m")
    assert [m["text"] for m in result["memories"]] == ["留下來的一條決定內容"]
    assert [m["text"] for m in result["below_threshold"]] == ["太沒把握的一條"]


def test_run_writes_tagged_memories_once(distill, monkeypatch, tmp_path):
    transcript = _transcript(
        tmp_path / "s.jsonl", [{"type": "user", "message": {"content": "用 pnpm"}}]
    )
    distill.record_transcript("sess", str(transcript), str(tmp_path / "my-app"))
    seen = {}

    def fake_distill(turns, project, model, already_kept=(), context=()):
        seen.update(turns=list(turns), context=list(context), already_kept=list(already_kept))
        return {
            "memories": [_memory("一律用 pnpm 管理套件", why="CI 會壞")],
            "below_threshold": [],
            "rejected": [],
            "parts": 1,
        }

    written = []
    monkeypatch.setattr(distill, "distill", fake_distill)
    monkeypatch.setattr(
        distill,
        "_remember",
        lambda ds, m, tags: written.append((ds, distill.content(m), tags)) or {"ok": True},
    )
    monkeypatch.delenv("COGNEE_PROJECT_NODE_SET", raising=False)

    assert distill.run("ds", "sess", trigger="final")["ok"]
    ((dataset, text, tags),) = written
    assert dataset == "ds" and text == "一律用 pnpm 管理套件\nWhy: CI 會壞"
    assert tags[0].startswith("project-my-app-") and tags[1] == "decision"

    # Unchanged transcript: nothing is distilled or written again.
    assert distill.run("ds", "sess", trigger="idle")["ok"]
    assert len(written) == 1

    # Grown transcript: only the new turn is read, the earlier one is context,
    # and the model is told what was already written.
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write("\n" + json.dumps({"type": "user", "message": {"content": "還有一件事"}}))
    distill.run("ds", "sess", trigger="idle")
    assert seen["turns"] == ["[U2] 還有一件事"]
    assert seen["context"] == ["[U1] 用 pnpm"]
    assert seen["already_kept"] == ["一律用 pnpm 管理套件"]

    # A cleared transcript is read from the start again.
    transcript.write_text(json.dumps({"type": "user", "message": {"content": "新的開始"}}), "utf-8")
    distill.run("ds", "sess", trigger="idle")
    assert seen["turns"] == ["[U1] 新的開始"] and seen["context"] == []


def test_context_is_the_newest_turns_within_the_cap(distill, monkeypatch):
    monkeypatch.setattr(distill, "CONTEXT_CHARS", 10)
    assert distill._context(["a" * 4, "b" * 4, "c" * 4]) == ["b" * 4, "c" * 4]
    assert distill._context([]) == []


def test_context_reaches_only_the_first_part(distill, monkeypatch):
    prompts = []
    monkeypatch.setattr(distill, "chunk", lambda turns: list(turns))  # one part per turn
    monkeypatch.setattr(
        distill,
        "ask_claude",
        lambda p, m, s=None: prompts.append(p) or {"memories": [], "rejected": []},
    )
    distill.distill(["[U2] one", "[U3] two"], "proj", "m", context=["[U1] earlier"])
    assert len(prompts) == 2
    assert "[U1] earlier" in prompts[0] and "[U1] earlier" not in prompts[1]


def test_run_keeps_unwritten_for_retry(distill, monkeypatch, tmp_path):
    transcript = _transcript(tmp_path / "s.jsonl", [{"type": "user", "message": {"content": "x"}}])
    distill.record_transcript("sess", str(transcript), str(tmp_path))
    monkeypatch.setattr(
        distill,
        "distill",
        lambda *a, **k: {
            "memories": [_memory("一條會寫失敗的記憶")],
            "below_threshold": [],
            "rejected": [],
            "parts": 1,
        },
    )
    monkeypatch.setattr(distill, "_remember", lambda *a: {"ok": False, "error": "unreachable"})
    result = distill.run("ds", "sess", trigger="final")
    assert not result["ok"] and result["reason"] == "failed"
    calls = []
    monkeypatch.setattr(distill, "_remember", lambda *a: calls.append(a) or {"ok": True})
    assert distill.run("ds", "sess", trigger="final")["ok"]  # size not recorded, so it runs again
    assert len(calls) == 1


def test_run_without_transcript_is_a_no_op(distill):
    assert distill.run("ds", "unknown", trigger="final") == {
        "ok": True,
        "reason": "no_transcript",
        "error": "",
    }


def test_improve_is_replaced_by_distill(suite, isolated_modules, monkeypatch):
    if suite.name != "claude-code":
        pytest.skip("session distill ships with the Claude Code plugin only")
    common = isolated_modules(suite, "_plugin_common")
    import _distill

    monkeypatch.setenv("COGNEE_DISTILL", "true")  # the test env turns it off

    calls = []
    monkeypatch.setattr(common, "_backend_reachable", lambda url: True)
    monkeypatch.setattr(
        _distill,
        "run",
        lambda ds, sid, trigger: calls.append(trigger) or {"ok": True, "reason": "", "error": ""},
    )
    monkeypatch.setattr(
        common, "improve_session_via_http", lambda *a: pytest.fail("improve must not run")
    )
    assert common.run_session_improve_detailed("ds", "s", trigger="auto")["ok"]
    assert common.run_session_improve_detailed("ds", "s", trigger="final")["ok"]
    assert calls == ["final"]
    # A distill arms the same cooldown an improve does.
    assert common.read_improve_state("s")["trigger"] == "final"


def test_multipart_repeats_list_fields(suite, isolated_modules):
    if suite.name != "claude-code":
        pytest.skip("list node sets are sent by session distill, Claude Code only")
    rh = isolated_modules(suite, "_remember_http")
    body, _ = rh._multipart_body({"node_set": ["a", "b"]}, [])
    assert body.count(b'name="node_set"') == 2
    assert rh.text_upload_name(["a", "b"], "x").startswith("a-")


@pytest.mark.parametrize(
    "value,expected",
    [
        ("", "sonnet"),
        ("haiku", "haiku"),
        ("claude-opus-5-5", "claude-opus-5-5"),
        ("sonnet[1m]", "sonnet[1m]"),
        ("--dangerous", "sonnet"),
        ("so nnet", "sonnet"),
    ],
)
def test_model_from_env_falls_back_when_unusable(distill, monkeypatch, value, expected):
    monkeypatch.setenv("COGNEE_DISTILL_MODEL", value)
    assert distill._model() == expected

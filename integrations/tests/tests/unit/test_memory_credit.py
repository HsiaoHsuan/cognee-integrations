"""Crediting Cognee memory in the answer (SDK-1071).

The recall hook tags every injected item (``m1``, ``m2``…) with where it came
from and asks the agent to end an answer that used memory with one
``🧠 Cognee memory [mN]: …`` line. Tags are numbered across the session and a
memory recalled again keeps its tag, and the Stop hook checks the answer's
credit line against the session's tags: valid credits and invented ones are
both logged, which is how we measure whether agents credit memory reliably.

Contract:
  * graph memory is tagged per passage, plus one tag each for entities and
    facts; a passage's label names the agent session or document it came from;
  * a memory recalled again in the session keeps its tag: an evidence item by
    its citation, an earlier turn or trace by its server id, anything else by
    its text;
  * where the host trims memory to a size budget or skips what is already in
    context, only the tags that reach the context count as this prompt's;
  * the instruction rides only on turns that injected something, says how many
    memories this prompt found, and keeps the longer first-credit wording until
    an answer has credited memory once;
  * only the credit line counts: ``arr[m1]`` in code is not a citation;
  * every prompt starts a new turn before recall can return early, so a cited
    tag from an earlier turn is logged as ``earlier``, never as this turn's;
  * an empty recall carries no instruction and no tags;
  * ``COGNEE_PLUGIN_CITE_MEMORY=off`` turns all of it off, and Codex starts
    with it off.

``_recall_text``'s credit section is the same in every suite; the hook tests run
for the suites whose recall hook uses it (claude-code and codex).
"""

from __future__ import annotations

import re

import pytest
from utils.recall import drive_recall

SESSION_KEY = "sess-credit-1"
CODEX_SID = "codex_01a0cdda-8d14-7291-bae3-c7eeb7fce251"
CLAUDE_SID = "claude_18c06095-db5a-4356-9d7b-16127c2f3190"

PASSAGE_CODEX = f"Session ID: {CODEX_SID}\n\nQuestion: CLI or server?\n\nAnswer: prefer the server."
PASSAGE_LEARNING = (
    f"# Session learning — 2026-10-03 (session {CLAUDE_SID})\n\nRun ruff before committing."
)
PASSAGE_DOC = "Cognee blog: memory for agents."
CONTEXT = (
    "## Relevant passages\n"
    + "\n---\n".join([PASSAGE_CODEX, PASSAGE_LEARNING, PASSAGE_DOC])
    + "\n\n## Relevant entities\n### server\n- server is preferred over the CLI.\n"
    + "\n## Related facts\n- the plugin prefers the server."
)
HIT = {"graph": [{"source": "graph", "text": CONTEXT}]}


@pytest.fixture
def rt(suite, isolated_modules):
    return isolated_modules(suite, "_recall_text")


# ── tagging ────────────────────────────────────────────────────────────────


def test_graph_memory_is_tagged_per_passage_with_its_origin(rt):
    tags = rt.MemoryTags()
    text = tags.tag_graph(CONTEXT)
    assert f"[m1 · Codex session]\n{PASSAGE_CODEX}" in text
    assert f"[m2 · Claude Code session learning, 2026-10-03]\n{PASSAGE_LEARNING}" in text
    assert f"[m3 · document]\n{PASSAGE_DOC}" in text
    assert "## Relevant entities [m4 · knowledge graph entities]\n### server" in text
    assert "## Related facts [m5 · knowledge graph facts]\n- the plugin prefers" in text
    assert [r["tag"] for r in tags.records] == ["m1", "m2", "m3", "m4", "m5"]
    assert tags.records[0]["snippet"].startswith("Session ID: codex_")


def test_a_memory_recalled_again_keeps_its_tag(rt, tmp_path):
    first = rt.MemoryTags()
    first.tag_graph(CONTEXT)
    first.save(tmp_path, SESSION_KEY)
    rt.begin_turn(tmp_path, SESSION_KEY)

    again = rt.MemoryTags.load(tmp_path, SESSION_KEY)
    new = "Session ID: codex_new\n\nQuestion: tabs?\n\nAnswer: spaces."
    text = again.tag_graph("## Relevant passages\n" + "\n---\n".join([PASSAGE_DOC, new]))
    assert "[m3 · document]" in text  # seen last turn
    assert "[m6 · Codex session]" in text  # new: the next number
    assert again.found == ["m3", "m6"]


def test_a_server_id_identifies_a_memory_whatever_its_text(rt):
    tags = rt.MemoryTags()
    assert tags.label("session", "earlier session turn", "Q: a\nA: b", "qa-1") == (
        "[m1 · earlier session turn]"
    )
    assert tags.add("session", "earlier session turn", "Q: a\nA: b, edited", "qa-1") == "m1"
    assert tags.add("session", "earlier session turn", "Q: a\nA: b, edited") == "m2"


def test_unsectioned_context_is_one_item(rt):
    tags = rt.MemoryTags()
    assert tags.tag_graph("bare context") == "[m1 · Cognee memory]\nbare context"
    assert tags.tag_graph("  ") == "  "
    assert len(tags.records) == 1


def _session_tags(rt, this_turn=("m1", "m2"), earlier=("m3",)):
    """Tags of a session on turn 2: ``this_turn`` injected now, ``earlier`` on turn 1."""
    records = [{"tag": t, "id": t, "turn": 2} for t in this_turn]
    records += [{"tag": t, "id": t, "turn": 1} for t in earlier]
    return rt.MemoryTags({"turn": 2, "tags": records})


@pytest.mark.parametrize(
    ("answer", "valid", "invented", "earlier"),
    [
        ("done.\n> 🧠 Cognee memory [m2]: you chose the server.", ["m2"], [], []),
        ("> 🧠 Cognee memory [m1, m3]: …", ["m1", "m3"], [], ["m3"]),
        ("> 🧠 Cognee memory [m9]: made up", [], ["m9"], []),
        ("Cognee memory [m1] and again Cognee memory [m1]", ["m1"], [], []),
        ("no credit here [x1] [m]", [], [], []),
        # Not a credit line: code, and the instruction's own example quoted back.
        ("use `arr[m1]`; cite several tags as [m1, m3]", [], [], []),
    ],
)
def test_check_credits(rt, answer, valid, invented, earlier):
    result = rt.check_credits(answer, _session_tags(rt))
    assert (result["valid"], result["invented"], result["earlier"]) == (valid, invented, earlier)
    assert result["available"] == 2


def test_credit_state_round_trip(rt, tmp_path):
    assert not rt.has_credited(tmp_path, SESSION_KEY)
    rt.record_credit(tmp_path, SESSION_KEY, {"valid": []})
    assert not rt.has_credited(tmp_path, SESSION_KEY)  # an invented-only turn doesn't count
    rt.record_credit(tmp_path, SESSION_KEY, {"valid": ["m1"]})
    assert rt.has_credited(tmp_path, SESSION_KEY)
    assert (tmp_path / "sessions" / SESSION_KEY / "credit.json").is_file()


@pytest.mark.parametrize("key", ["../escape", "..", "", "a/b"])
def test_unsafe_session_keys_write_nothing(rt, tmp_path, key):
    state = tmp_path / "state"
    tags = rt.MemoryTags()
    tags.add("memory", "Cognee memory", "x")
    tags.save(state, key)
    rt.begin_turn(state, key)
    rt.record_credit(state, key, {"valid": ["m1"]})
    assert not any(tmp_path.rglob("*.json"))


# ── the recall hook ────────────────────────────────────────────────────────


@pytest.fixture
def lookup(suite, hook_module, monkeypatch, rt):
    # ``rt`` first: loading a module scrubs COGNEE_* from the environment.
    if suite.name not in ("claude-code", "codex"):
        pytest.skip(f"{suite.name}: recall hook does not credit memory")
    module = hook_module(suite, "session-context-lookup.py")
    monkeypatch.setattr(module, "get_session_key", lambda: SESSION_KEY)
    monkeypatch.setenv("COGNEE_PLUGIN_CITE_MEMORY", "on")  # Codex starts with it off
    return module


def _context(run) -> str:
    return run.output["hookSpecificOutput"]["additionalContext"]


def _state_dir(lookup):
    return lookup._plugin_common._PLUGIN_DIR


def _saved(rt, lookup):
    return rt.MemoryTags.load(_state_dir(lookup), SESSION_KEY)


def _tag_of(ctx: str, origin: str) -> str:
    """The tag ``ctx`` shows for the item labelled ``origin``."""
    match = re.search(rf"\[(m\d+) · {re.escape(origin)}\]", ctx)
    assert match, f"no [mN · {origin}] in context"
    return match.group(1)


def _assert_counted_as_shown(ctx: str, tags) -> None:
    """Exactly the tags visible in the context count as this turn's, and the
    instruction states that number."""
    shown = {r["tag"] for r in tags.records if r.get("turn") == tags.turn}
    visible = {t for t in re.findall(r"\[(m\d+) · ", ctx)}
    assert shown == visible
    if "Crediting Cognee memory" in ctx:  # a tiny size budget can leave it out
        noun = "1 memory" if len(shown) == 1 else f"{len(shown)} memories"
        assert f"Cognee found {noun} for this prompt" in ctx


def test_hit_is_tagged_and_carries_the_first_credit_instruction(lookup, monkeypatch, rt):
    ctx = _context(drive_recall(lookup, monkeypatch, recall=HIT))
    _tag_of(ctx, "Codex session")
    _tag_of(ctx, "Claude Code session learning, 2026-10-03")
    _tag_of(ctx, "knowledge graph entities")
    assert "Cognee found 5 memories for this prompt" in ctx
    assert "> 🧠 Cognee memory [mN]:" in ctx
    assert "first Cognee credit in this session" in ctx
    tags = _saved(rt, lookup)
    assert len(tags.records) == 5
    _assert_counted_as_shown(ctx, tags)


def test_a_memory_recalled_on_a_later_prompt_keeps_its_tag(lookup, monkeypatch, rt):
    first = _context(drive_recall(lookup, monkeypatch, recall=HIT))
    codex_tag = _tag_of(first, "Codex session")
    new = "Session ID: codex_new\n\nQuestion: tabs?\n\nAnswer: spaces."
    more = {
        "graph": [
            {"source": "graph", "text": CONTEXT.replace(PASSAGE_DOC, f"{PASSAGE_DOC}\n---\n{new}")}
        ]
    }
    second = _context(drive_recall(lookup, monkeypatch, recall=more))
    tags = _saved(rt, lookup)
    assert len(tags.records) == 6  # one new memory, five known ones
    assert next(
        r["tag"] for r in tags.records if r["snippet"].startswith("Session ID: codex_01")
    ) == (codex_tag)
    # Shown again it keeps its tag; a host that skips memory already in context
    # does not show it, and then it does not count as this prompt's.
    if PASSAGE_CODEX in second:
        assert f"[{codex_tag} · Codex session]" in second
    assert "[m6 · Codex session]" in second
    _assert_counted_as_shown(second, tags)


def test_an_earlier_turn_keeps_its_tag_by_server_id(lookup, monkeypatch, rt):
    turn = {"source": "session", "qa_id": "qa-7", "question": "q", "answer": "a"}
    edited = dict(turn, answer="a, with feedback")
    first = _context(drive_recall(lookup, monkeypatch, recall={"session_context": [turn]}))
    second = _context(drive_recall(lookup, monkeypatch, recall={"session_context": [edited]}))
    assert "[m1 · earlier session turn]" in first
    assert "[m1 · earlier session turn]" in second


def test_after_a_credit_the_short_instruction(lookup, monkeypatch, rt):
    rt.record_credit(_state_dir(lookup), SESSION_KEY, {"valid": ["m1"]})
    ctx = _context(drive_recall(lookup, monkeypatch, recall=HIT))
    assert "> 🧠 Cognee memory [mN]:" in ctx
    assert "first Cognee credit" not in ctx


def test_empty_recall_has_no_instruction_and_no_tags_for_the_turn(lookup, monkeypatch, rt):
    drive_recall(lookup, monkeypatch, recall=HIT)
    run = drive_recall(lookup, monkeypatch, recall={})
    ctx = _context(run)
    assert "Crediting Cognee memory" not in ctx and "[m" not in ctx
    assert run.output["hookSpecificOutput"].get("systemMessage") or run.output.get("systemMessage")
    assert rt.check_credits("", _saved(rt, lookup))["available"] == 0


def test_a_recall_that_returns_early_still_starts_a_new_turn(lookup, monkeypatch, rt):
    """Server down, no session or a timeout: the hook returns before tagging, and
    the previous turn's tags must not pass for this turn's."""
    import asyncio

    drive_recall(lookup, monkeypatch, recall=HIT)
    monkeypatch.setattr(lookup, "_load_session_id", lambda: "")
    assert asyncio.run(lookup._run("next prompt")) is None
    result = rt.check_credits("> 🧠 Cognee memory [m2]: …", _saved(rt, lookup))
    assert (result["valid"], result["earlier"], result["available"]) == (["m2"], ["m2"], 0)


def test_opt_out(lookup, monkeypatch, rt):
    monkeypatch.setenv("COGNEE_PLUGIN_CITE_MEMORY", "off")
    ctx = _context(drive_recall(lookup, monkeypatch, recall=HIT))
    assert not re.search(r"\[m\d+ · ", ctx) and "🧠" not in ctx
    assert PASSAGE_CODEX in ctx  # memory itself is unchanged


def test_default_per_host(suite, lookup, monkeypatch):
    """On by default in Claude Code; off in Codex, whose terminal shows the injected
    instruction, until it has been tried live."""
    monkeypatch.delenv("COGNEE_PLUGIN_CITE_MEMORY")
    ctx = _context(drive_recall(lookup, monkeypatch, recall=HIT))
    assert bool(re.search(r"\[m\d+ · ", ctx)) is suite.cites_memory_by_default


# ── the Stop hook ──────────────────────────────────────────────────────────


@pytest.fixture
def stop(suite, hook_module, monkeypatch, rt):
    if suite.name not in ("claude-code", "codex"):
        pytest.skip(f"{suite.name}: no memory credit check")
    module = hook_module(suite, "store-to-session.py")
    events: list = []
    monkeypatch.setenv("COGNEE_PLUGIN_CITE_MEMORY", "on")
    monkeypatch.setattr(module, "get_session_key", lambda: SESSION_KEY)
    monkeypatch.setattr(module, "hook_log", lambda e, d=None: events.append((e, d or {})))
    module.events = events
    return module


def _turn(rt, stop, answer, this_turn=("m1", "m2"), earlier=()):
    import _plugin_common

    state = _plugin_common._PLUGIN_DIR
    _session_tags(rt, this_turn, earlier).save(state, SESSION_KEY)
    stop._check_memory_credit({"last_assistant_message": answer})
    return state


def test_stop_logs_valid_and_invented_credits(stop, rt):
    state = _turn(rt, stop, "ok\n> 🧠 Cognee memory [m2, m7]: you chose the server.")
    assert stop.events == [
        (
            "memory_credit",
            {
                "cited": ["m2", "m7"],
                "valid": ["m2"],
                "invented": ["m7"],
                "earlier": [],
                "available": 2,
            },
        )
    ]
    assert rt.has_credited(state, SESSION_KEY)


def test_stop_checks_an_earlier_turns_tag_against_its_own_memory(stop, rt):
    _turn(rt, stop, "> 🧠 Cognee memory [m3]: …", this_turn=(), earlier=("m3",))
    assert stop.events[0][1]["valid"] == ["m3"]
    assert stop.events[0][1]["earlier"] == ["m3"]


def test_stop_without_credit_logs_an_uncredited_turn(stop, rt):
    state = _turn(rt, stop, "a plain answer")
    assert stop.events[0][1]["cited"] == []
    assert not rt.has_credited(state, SESSION_KEY)


def test_stop_is_silent_when_nothing_was_injected_or_cited(stop, rt):
    _turn(rt, stop, "a plain answer", this_turn=())
    assert stop.events == []


def test_stop_logs_a_citation_on_a_turn_without_memory_as_invented(stop, rt):
    _turn(rt, stop, "> 🧠 Cognee memory [m1]: …", this_turn=())
    assert stop.events[0][1]["invented"] == ["m1"]


def test_instruction_comes_before_the_memory(lookup, monkeypatch):
    """Claude Code shows the model only a 2 KB preview of hook context over 10,000
    characters, so the instruction must come before the memory, not after it."""
    big = {"graph": [{"source": "graph", "text": CONTEXT + "\n- filler fact." * 1500}]}
    ctx = _context(drive_recall(lookup, monkeypatch, recall=big))
    at = ctx.index("> 🧠 Cognee memory [mN]:")
    assert at < re.search(r"\[m\d+ · ", ctx).start()
    assert ctx.index("first Cognee credit") < 2_000


def test_memory_trimmed_to_fit_is_not_counted(lookup, monkeypatch, rt):
    """A host that trims memory to a size budget tags before trimming: only the
    tags that made it into the context count as this prompt's."""
    monkeypatch.setenv("COGNEE_INJECT_MAX_CHARS", "1024")
    passages = [f"Cognee doc {i}: " + "detail " * 40 for i in range(12)]
    text = "## Relevant passages\n" + "\n---\n".join(passages)
    ctx = _context(
        drive_recall(lookup, monkeypatch, recall={"graph": [{"source": "graph", "text": text}]})
    )
    _assert_counted_as_shown(ctx, _saved(rt, lookup))


# ── "this conversation was saved" on resume ────────────────────────────────

NOW = 1_791_820_000.0  # a fixed "now"; earlier times below are the same day


def test_notice_for_a_final_save(rt):
    message, ts = rt.sync_notice({"last_improved_at": NOW - 600, "trigger": "final"}, now=NOW)
    assert message.startswith(
        "🧠 This conversation was sent to Cognee's long-term memory when you left at "
    )
    assert ts == NOW - 600


def test_notice_for_an_idle_save(rt):
    message, _ = rt.sync_notice({"last_improved_at": NOW - 600, "trigger": "idle"}, now=NOW)
    assert "long-term memory at " in message and "when you left" not in message


def test_notice_for_a_failed_final_save_mentions_the_earlier_one(rt):
    state = {
        "last_improved_at": NOW - 3600,
        "trigger": "idle",
        "last_failed_at": NOW - 600,
        "last_failure_reason": "unreachable",
        "last_failure_trigger": "final",
    }
    message, ts = rt.sync_notice(state, now=NOW)
    assert message.startswith("🧠 This conversation wasn't saved to Cognee when you left at ")
    assert "the Cognee server was unreachable" in message
    assert "An earlier save at " in message
    assert ts == NOW - 600


def test_no_record_no_notice(rt):
    assert rt.sync_notice({}, now=NOW) == ("", 0.0)
    assert rt.sync_notice({"last_improved_at": "junk"}, now=NOW) == ("", 0.0)


def test_another_day_shows_the_date(rt):
    message, _ = rt.sync_notice({"last_improved_at": NOW - 3 * 86400, "trigger": "final"}, now=NOW)
    assert any(
        month in message
        for month in (
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        )
    )


def test_notice_is_shown_once_per_save(rt, tmp_path):
    assert rt.notice_due(tmp_path, "claude_s1", 100.0)
    rt.mark_notice_shown(tmp_path, "claude_s1", 100.0)
    assert not rt.notice_due(tmp_path, "claude_s1", 100.0)
    assert rt.notice_due(tmp_path, "claude_s1", 200.0)  # a later save is news again
    assert not rt.notice_due(tmp_path, "", 100.0)


@pytest.fixture
def start(suite, hook_module, monkeypatch):
    if suite.name not in ("claude-code", "codex"):
        pytest.skip(f"{suite.name}: no resume notice")
    module = hook_module(suite, "session-start.py")
    import config

    monkeypatch.setenv("COGNEE_PLUGIN_CITE_MEMORY", "on")
    monkeypatch.setattr(config, "get_session_id", lambda *a, **k: "claude_resumed")
    return module


RESUME = {"source": "resume"}


def test_resume_shows_the_notice_once(start):
    import _plugin_common

    _plugin_common.record_improve_success("claude_resumed", "agent_sessions", "final")
    out = start._apply_sync_notice(
        {"hookSpecificOutput": {"hookEventName": "SessionStart"}}, RESUME
    )
    # Top level is what Claude Code shows the user; additionalContext reaches the agent.
    assert out["systemMessage"].startswith("🧠 This conversation was sent to Cognee")
    assert out["hookSpecificOutput"]["additionalContext"] == out["systemMessage"]
    again = start._apply_sync_notice({}, RESUME)
    assert again == {}


@pytest.mark.parametrize("source", ["compact", "startup", "clear", ""])
def test_only_a_resume_shows_the_notice(start, source):
    """A compaction keeps the session too, but lands mid-task."""
    import _plugin_common

    _plugin_common.record_improve_success("claude_resumed", "agent_sessions", "idle")
    assert start._apply_sync_notice({}, {"source": source}) == {}
    assert start._apply_sync_notice({}, RESUME)["systemMessage"]  # still due


def test_a_new_chat_shows_nothing(start):
    assert start._apply_sync_notice({}, RESUME) == {}

"""Tests for the Python client and the exam tools. Standard library only.

    python3 -m unittest discover -s test/py -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT.parents[1] / "integrations" / "claude-code" / "scripts"))
sys.path.insert(0, str(ROOT / "eval"))

import _intent_gate as intent_gate  # noqa: E402
import make_exam  # noqa: E402
import run_exam  # noqa: E402

DROP = {"save": False, "choice": "command", "confidence": 0.93, "node_set": None, "source": "jev"}
KEEP = {"save": True, "choice": "spec_rule", "confidence": 0.86, "node_set": "ffmpeg", "source": "jev"}


class FakeGate:
    """A local HTTP server standing in for the Worker."""

    def __init__(self):
        self.requests: list[dict] = []
        self.reply = (200, DROP)
        self.delay = 0.0
        gate = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                gate.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                if gate.delay:
                    threading.Event().wait(gate.delay)
                status, payload = gate.reply
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except BrokenPipeError:
                    pass

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.gate = FakeGate()
        self.addCleanup(self.gate.close)
        self.logged: list[tuple] = []

    def judge(self, text="CI 過了就 merge", **kwargs):
        kwargs.setdefault("url", self.gate.url)
        kwargs.setdefault("token", "secret")
        return intent_gate.judge(text, log=lambda event, detail: self.logged.append((event, detail)), **kwargs)

    def test_returns_the_gate_verdict(self):
        self.assertEqual(self.judge(), DROP)
        self.assertEqual(self.logged, [])

    def test_request_shape(self):
        self.judge(context="要 A 還是 B？", project="ffmpeg", key="sess:p1")
        request = self.gate.requests[0]
        self.assertEqual(request["path"], "/v1/judge")
        self.assertEqual(request["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(
            request["body"],
            {"text": "CI 過了就 merge", "context": "要 A 還是 B？", "project": "ffmpeg", "key": "sess:p1"},
        )

    def test_names_itself_because_workers_dev_blocks_python_urllib(self):
        self.judge()
        agent = self.gate.requests[0]["headers"]["User-Agent"]
        self.assertEqual(agent, intent_gate.USER_AGENT)
        self.assertNotIn("urllib", agent.lower())

    def test_blank_text_is_a_caller_error(self):
        for bad in ("", "   ", None, 42):
            with self.assertRaises(ValueError):
                self.judge(bad)
        self.assertEqual(self.gate.requests, [])

    def assertFallback(self, verdict, project=None):
        self.assertEqual(
            verdict,
            {"save": True, "choice": "project_fact", "confidence": 0, "node_set": project, "source": "fallback"},
        )

    def test_http_errors_keep_the_prompt(self):
        for status in (400, 401, 500, 503):
            self.gate.reply = (status, {"error": "x"})
            self.assertFallback(self.judge(project="ffmpeg"), "ffmpeg")
        self.assertTrue(all(event == "intent_gate_failed" for event, _ in self.logged))
        self.assertEqual(len(self.logged), 4)

    def test_unreachable_gate_keeps_the_prompt(self):
        self.gate.close()
        self.assertFallback(self.judge())

    def test_a_url_without_a_scheme_keeps_the_prompt(self):
        self.assertFallback(self.judge(url="intent-gate.example.workers.dev"))
        self.assertEqual(self.logged[0][0], "intent_gate_failed")

    def test_slow_gate_keeps_the_prompt(self):
        self.gate.delay = 1.0
        self.assertFallback(self.judge(timeout=0.2))

    def test_unconfigured_gate_keeps_the_prompt(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFallback(intent_gate.judge("CI 過了就 merge"))

    def test_malformed_verdicts_keep_the_prompt(self):
        bad = [
            b"not json",
            [],
            {**DROP, "save": "no"},
            {**DROP, "choice": "made_up"},
            {**DROP, "source": "magic"},
            {**DROP, "confidence": 7},
            {**DROP, "confidence": True},
            {**DROP, "node_set": "ffmpeg"},  # dropped but carries a node_set
            {**DROP, "choice": "spec_rule"},  # a drop the table can never produce
            {**KEEP, "node_set": None},  # kept with nowhere to put it
        ]
        for payload in bad:
            self.gate.reply = (200, payload)
            self.assertFallback(self.judge(), None)
        self.assertEqual(len(self.logged), len(bad))

    def test_a_server_side_fallback_is_passed_through_and_logged(self):
        self.gate.reply = (200, intent_gate.fallback("ffmpeg"))
        self.assertFallback(self.judge(project="ffmpeg", key="k"), "ffmpeg")
        self.assertEqual(self.logged, [("intent_gate_fallback", {"key": "k"})])

    def test_debug_asks_for_details(self):
        self.gate.reply = (200, {**DROP, "debug": {"raw": {"choice": "command", "confidence": 0.93}}})
        verdict = self.judge(debug=True)
        self.assertEqual(self.gate.requests[0]["path"], "/v1/judge?debug=1")
        self.assertEqual(verdict["debug"]["raw"]["choice"], "command")


class HelpersTest(unittest.TestCase):
    def test_judge_is_off_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(intent_gate.judge_enabled())
        for value in ("true", "1", "YES", "on"):
            with mock.patch.dict(os.environ, {"COGNEE_CAPTURE_JUDGE": value}):
                self.assertTrue(intent_gate.judge_enabled())
        with mock.patch.dict(os.environ, {"COGNEE_CAPTURE_JUDGE": "false"}):
            self.assertFalse(intent_gate.judge_enabled())

    def test_turn_key_is_the_same_for_every_hook_of_a_turn(self):
        self.assertEqual(intent_gate.turn_key({"session_id": "s", "prompt_id": "p"}), "s:p")
        self.assertEqual(intent_gate.turn_key({"session_id": "s", "turn_id": "t"}), "s:t")
        self.assertIsNone(intent_gate.turn_key({"session_id": "s"}))
        self.assertIsNone(intent_gate.turn_key({"prompt_id": "p"}))

    def test_last_assistant_text(self):
        rows = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "old reply"}]}},
            {"type": "user", "message": {"content": "next"}},
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": "A 還是 B？"}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            {"type": "system", "content": "x"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n{"type": "assis', encoding="utf-8")
            self.assertEqual(intent_gate.last_assistant_text(str(path)), "A 還是 B？")
            self.assertEqual(intent_gate.last_assistant_text(str(path), max_chars=3), " B？")
            self.assertIsNone(intent_gate.last_assistant_text(str(Path(tmp) / "missing.jsonl")))
        self.assertIsNone(intent_gate.last_assistant_text(None))

    def test_last_assistant_text_reads_only_the_tail_of_a_big_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.jsonl"
            filler = json.dumps({"type": "user", "message": {"content": "x" * 2000}})
            last = json.dumps({"type": "assistant", "message": {"content": "the end"}})
            path.write_text("\n".join([filler] * 600 + [last]) + "\n", encoding="utf-8")
            self.assertGreater(path.stat().st_size, 1024 * 1024)
            self.assertEqual(intent_gate.last_assistant_text(str(path)), "the end")


class RuleParityTest(unittest.TestCase):
    """make_exam.py skips what src/rules.ts decides; the two must agree."""

    def test_same_cases_as_the_typescript_rule_tests(self):
        for text in ("好", "繼續", "ok", "OK!", "好的。", "lgtm", "謝謝", "/clear", "/compact", "/cognee:recap", " /clear "):
            self.assertTrue(make_exam.decided_by_rule(text), text)
        for text in ("B + 2", "CI 過了就 merge", "好，但是匯出改成底部彈出", "手機版的", "/workspaces/ffmpeg/截圖.png 紅色那個區域縮短",
                     "/tmp 不要放暫存檔，一律用 scratchpad", "/src 底下一律用 TypeScript strict", "/README.md", "/model opus"):
            self.assertFalse(make_exam.decided_by_rule(text), text)


class MakeExamTest(unittest.TestCase):
    def test_samples_history_and_finds_the_preceding_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            history = tmp / "history.jsonl"
            rows = [
                {"display": "B + 2", "project": "/workspaces/ffmpeg", "sessionId": "s1"},
                {"display": "B + 2", "project": "/workspaces/ffmpeg", "sessionId": "s1"},  # duplicate
                {"display": "好", "project": "/workspaces/ffmpeg", "sessionId": "s1"},  # rule layer
                {"display": "/clear", "project": "/workspaces/ffmpeg", "sessionId": "s1"},  # rule layer
                {"display": "CI 過了就 merge", "project": "/workspaces/joy-app", "sessionId": "gone"},
            ]
            history.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
            session = tmp / "projects" / "-workspaces-ffmpeg"
            session.mkdir(parents=True)
            transcript = [
                {"type": "user", "message": {"content": "排版怎麼做"}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "A 單頁式還是 B 分頁式？匯出 1 或 2？"}]}},
                {"type": "user", "message": {"content": "<system-reminder>ignored</system-reminder>"}},
                {"type": "user", "message": {"content": "B + 2"}},
                {"type": "assistant", "message": {"content": "好，照 B + 2 做"}},
            ]
            (session / "s1.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in transcript), encoding="utf-8")
            out = tmp / "exam.jsonl"
            argv = ["make_exam.py", "-n", "10", "-o", str(out), "--history", str(history), "--projects", str(tmp / "projects")]
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                make_exam.main()
            exam = {r["text"]: r for r in map(json.loads, out.read_text(encoding="utf-8").splitlines())}
            self.assertEqual(set(exam), {"B + 2", "CI 過了就 merge"})
            self.assertEqual(exam["B + 2"]["context"], "A 單頁式還是 B 分頁式？匯出 1 或 2？")
            self.assertEqual(exam["B + 2"]["project"], "ffmpeg")
            self.assertIsNone(exam["CI 過了就 merge"]["context"])
            self.assertEqual(exam["B + 2"]["intent"], "")
            self.assertEqual(run_exam.load_exam(str(out)), [], "unlabelled rows are ignored")


def result(intent, choice, save, source="jev", conf=0.9, raw=None, raw_conf=None):
    return {
        "text": f"{intent}->{choice}", "intent": intent, "labels": intent.split("|"), "choice": choice, "save": save,
        "source": source, "confidence": conf, "raw_choice": raw if raw is not None else choice,
        "raw_confidence": raw_conf if raw_conf is not None else conf, "note": "",
    }


class ScoringTest(unittest.TestCase):
    def report(self, results):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok = run_exam.report(results)
        return ok, buf.getvalue()

    def test_headline_numbers(self):
        results = [
            result("spec_rule", "spec_rule", True),
            result("decision", "project_fact", True),  # wrong label, still kept
            result("preference", "command", False),  # lost
            result("command", "command", False),
            result("question", "feature_req", True),  # noise kept
            result("ack", "ack", False, source="rule", conf=1),
        ]
        ok, text = self.report(results)
        self.assertFalse(ok)
        self.assertIn("keep recall     2/3 (67%)", text)
        self.assertIn("keep precision  2/3 (67%)", text)
        self.assertIn("dropped         3/6 (50%)", text)
        self.assertIn("intent accuracy 3/6 (50%)", text)
        self.assertIn("LOST: 1 prompt(s)", text)
        self.assertIn("preference -> command", text)

    def test_nothing_lost_passes(self):
        ok, text = self.report([result("spec_rule", "decision", True), result("command", "command", False)])
        self.assertTrue(ok)
        self.assertNotIn("LOST", text)

    def test_fallbacks_count_as_kept_but_not_towards_accuracy(self):
        results = [
            result("command", "project_fact", True, source="fallback", conf=0, raw="command", raw_conf=0.3),
            result("command", "command", False),
        ]
        ok, text = self.report(results)
        self.assertTrue(ok)
        self.assertIn("intent accuracy 1/1 (100%)", text)
        self.assertIn("kept although droppable: 1", text)

    def test_threshold_sweep_uses_what_jev_really_answered(self):
        results = [
            result("spec_rule", "command", False, conf=0.6),  # lost at 0.5, rescued at 0.7
            result("command", "command", False, conf=0.95),
        ]
        _, text = self.report(results)
        lines = {l.split()[0]: l for l in text.splitlines() if l.strip()[:3] in ("0.0", "0.5", "0.7", "0.9")}
        self.assertIn("0/1 (0%)", lines["0.5"])
        self.assertIn("1/1 (100%)", lines["0.7"])

    def test_multiple_acceptable_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "exam.jsonl"
            path.write_text(json.dumps({"text": "看一下 test", "intent": "command|question"}) + "\n", encoding="utf-8")
            (row,) = run_exam.load_exam(str(path))
            self.assertEqual(row["labels"], ["command", "question"])
            path.write_text(json.dumps({"text": "x", "intent": "command|decision"}) + "\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                run_exam.load_exam(str(path))
            path.write_text(json.dumps({"text": "x", "intent": "nonsense"}) + "\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                run_exam.load_exam(str(path))

    def test_shipped_exam_is_valid(self):
        rows = run_exam.load_exam(str(ROOT / "eval" / "known_hard.jsonl"))
        self.assertEqual(len(rows), 9)


class JudgesTest(unittest.TestCase):
    def test_gate_judge_reports_what_jev_said_even_on_fallback(self):
        gate = FakeGate()
        self.addCleanup(gate.close)
        gate.reply = (200, {**intent_gate.fallback("p"), "debug": {
            "fallback_reason": "low_confidence", "raw": {"choice": "command", "confidence": 0.31}}})
        with mock.patch.dict(os.environ, {"INTENT_GATE_URL": gate.url, "INTENT_GATE_TOKEN": "t"}):
            out = run_exam.judge_gate({"text": "CI 過了就 merge", "project": "p"})
        self.assertEqual((out["save"], out["source"], out["raw_choice"], out["raw_confidence"], out["note"]),
                         (True, "fallback", "command", 0.31, "low_confidence"))

    def fake_claude(self, tmp: Path, script: str):
        exe = tmp / "claude"
        exe.write_text("#!/bin/sh\n" + script, encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        return mock.patch.dict(os.environ, {"PATH": f"{tmp}{os.pathsep}{os.environ['PATH']}"})

    def test_haiku_baseline_uses_the_same_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            reply = json.dumps({"structured_output": {"intent": "command", "confidence": 0.8}})
            with self.fake_claude(Path(tmp), f"echo '{reply}'\n"):
                out = run_exam.judge_haiku({"text": "CI 過了就 merge", "project": "p"})
        self.assertEqual((out["save"], out["choice"], out["source"]), (False, "command", "haiku"))

    def test_haiku_baseline_survives_an_empty_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.fake_claude(Path(tmp), "echo 'rate limited' >&2\nexit 1\n"):
                out = run_exam.judge_haiku({"text": "CI 過了就 merge", "project": "p"})
        self.assertEqual((out["save"], out["source"]), (True, "fallback"))
        self.assertIn("empty stdout", out["note"])


if __name__ == "__main__":
    unittest.main()

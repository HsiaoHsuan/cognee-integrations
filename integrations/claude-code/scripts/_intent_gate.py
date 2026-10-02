"""Client for the intent-gate service. Standard library only.

Asks the gate whether a user prompt is worth keeping in long-term memory.
Drop this file next to a hook script (in the cognee plugin it is installed as
``scripts/_intent_gate.py``) or import it from any Python agent.

The one rule: this client never loses a prompt. If the gate is unreachable,
slow, misconfigured, or answers with something unexpected, the result is the
fail-open verdict (``save=True, source="fallback"``) and the caller stores the
prompt exactly as it did before the gate existed.

Environment:
    COGNEE_CAPTURE_JUDGE   "true" to enable (default: off; ``judge_enabled()``)
    INTENT_GATE_URL        e.g. https://intent-gate.<subdomain>.workers.dev
    INTENT_GATE_TOKEN      the GATE_TOKEN secret set on the Worker
    INTENT_GATE_TIMEOUT    seconds to wait for a verdict (default 30)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Callable, Optional

SAVE_INTENTS = ("spec_rule", "preference", "decision", "project_fact", "feature_req")
DROP_INTENTS = ("bug_report", "command", "question", "choice_reply", "ack", "chitchat")
INTENTS = SAVE_INTENTS + DROP_INTENTS

MAX_CONTEXT_CHARS = 1200
# workers.dev rejects the default "Python-urllib/x.y" agent (Cloudflare error
# 1010), so the client always names itself.
USER_AGENT = "intent-gate-client/0.1"

_TRANSCRIPT_TAIL_BYTES = 512 * 1024

Log = Callable[[str, dict], None]


def judge_enabled() -> bool:
    return os.environ.get("COGNEE_CAPTURE_JUDGE", "").strip().lower() in ("1", "true", "yes", "on")


def fallback(project: Optional[str] = None) -> dict:
    """The fail-open verdict: keep the prompt, the way it was kept before."""
    return {
        "save": True,
        "choice": "project_fact",  # placeholder, per contract
        "confidence": 0,
        "node_set": (project or "").strip() or None,
        "source": "fallback",
    }


def turn_key(payload: dict) -> Optional[str]:
    """A key every hook of the same turn can compute on its own.

    Claude Code puts ``session_id`` and ``prompt_id`` in every hook payload, so
    two hooks from different plugins derive the same key without talking to
    each other, and the gate hands both the same verdict.
    """
    session = str(payload.get("session_id") or "").strip()
    turn = str(payload.get("prompt_id") or payload.get("turn_id") or "").strip()
    return f"{session}:{turn}" if session and turn else None


def last_assistant_text(transcript_path, max_chars: int = MAX_CONTEXT_CHARS) -> Optional[str]:
    """Tail of the last assistant text in a Claude Code transcript, or None.

    Only the end of the file is read, so a long session costs the same as a
    short one. Any problem reading it just means "no context".
    """
    if not transcript_path:
        return None
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _TRANSCRIPT_TAIL_BYTES))
            chunk = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(chunk.splitlines()):
        try:
            row = json.loads(line)
        except ValueError:  # first line of the chunk may be cut; live files have partial lines
            continue
        if not isinstance(row, dict) or row.get("type") != "assistant":
            continue
        text = _content_text((row.get("message") or {}).get("content")).strip()
        if text:
            return text[-max_chars:]
    return None


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def judge(
    text: str,
    *,
    context: Optional[str] = None,
    project: Optional[str] = None,
    key: Optional[str] = None,
    url: Optional[str] = None,
    token: Optional[str] = None,
    timeout: Optional[float] = None,
    debug: bool = False,
    log: Optional[Log] = None,
) -> dict:
    """Return the gate's verdict for one prompt.

    Result: ``{"save", "choice", "confidence", "node_set", "source"}``.
    Raises ValueError for blank text (a caller bug). Every other problem
    returns ``fallback(project)`` and is reported through ``log``.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-blank string")
    log = log or (lambda event, detail: None)
    url = (url or os.environ.get("INTENT_GATE_URL", "")).strip().rstrip("/")
    token = (token or os.environ.get("INTENT_GATE_TOKEN", "")).strip()
    if timeout is None:
        timeout = _positive_float(os.environ.get("INTENT_GATE_TIMEOUT"), 30.0)

    if not url or not token:
        log("intent_gate_unconfigured", {"url": bool(url), "token": bool(token)})
        return fallback(project)

    body = {"text": text, "context": context or None, "project": project or None}
    if key:
        body["key"] = key
    try:
        request = urllib.request.Request(
            url + "/v1/judge" + ("?debug=1" if debug else ""),
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            verdict = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        log("intent_gate_failed", {"status": exc.code, "error": str(exc)[:200]})
        return fallback(project)
    except Exception as exc:  # bad URL, timeout, DNS, TLS, bad JSON: keep the prompt
        log("intent_gate_failed", {"error": f"{type(exc).__name__}: {exc}"[:200]})
        return fallback(project)

    problem = _verdict_problem(verdict)
    if problem:
        log("intent_gate_failed", {"error": "bad verdict: " + problem})
        return fallback(project)
    if verdict["source"] == "fallback":
        log("intent_gate_fallback", {"key": key})
    return verdict


def _verdict_problem(verdict) -> str:
    """Why a response is not a usable verdict; empty string when it is fine."""
    if not isinstance(verdict, dict):
        return "not an object"
    if not isinstance(verdict.get("save"), bool):
        return "save is not a boolean"
    if verdict.get("choice") not in INTENTS:
        return "unknown choice"
    if verdict.get("source") not in ("rule", "jev", "fallback"):
        return "unknown source"
    confidence = verdict.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        return "confidence out of range"
    node_set = verdict.get("node_set")
    if verdict["save"]:
        if not isinstance(node_set, str) or not node_set:
            return "save without node_set"
    elif node_set is not None:
        return "node_set on a dropped prompt"
    # A drop must come from a drop intent; anything else would lose data on a
    # verdict the table never produces.
    if not verdict["save"] and verdict["choice"] not in DROP_INTENTS:
        return "drop with a save intent"
    return ""


def _positive_float(raw, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 and value != float("inf") else default


if __name__ == "__main__":
    # Quick manual check:  python3 intent_gate.py "CI 過了就 merge" [project]
    import sys

    if len(sys.argv) < 2:
        sys.exit('usage: intent_gate.py "<text>" [project]')
    print(
        json.dumps(
            judge(
                sys.argv[1],
                project=sys.argv[2] if len(sys.argv) > 2 else None,
                debug=True,
                log=lambda event, detail: print(event, detail, file=sys.stderr),
            ),
            ensure_ascii=False,
            indent=2,
        )
    )

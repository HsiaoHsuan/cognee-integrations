#!/usr/bin/env python3
"""Draft an exam from your own Claude Code history, ready for hand labelling.

Samples unique prompts from ~/.claude/history.jsonl, looks up the assistant
reply that preceded each one, and writes one JSON object per line:

    {"text": ..., "project": ..., "context": ..., "intent": ""}

Fill in "intent" with one of the 11 labels (see --labels), then score the gate
with run_exam.py. Rows left blank are ignored. Prompts the rule layer already
decides (filler, slash commands) are left out: they never reach Jev.

The output contains your real prompts. Keep it out of public repos.

    python3 eval/make_exam.py -n 150 -o exam.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from pathlib import Path

# Same client the plugin ships, so the exam scores exactly what the hook sends.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "integrations" / "claude-code" / "scripts"))
from _intent_gate import INTENTS, MAX_CONTEXT_CHARS  # noqa: E402

# Mirrors src/rules.ts.
_FILLER = re.compile(r"^(ok|okay|好|好的|嗯|對|是|繼續|continue|yes|no|thanks|謝謝|go|y|n|lgtm)[!！。. ]*$", re.I)
_SLASH = re.compile(r"^/[\w:-]+$", re.A)


def decided_by_rule(text: str) -> bool:
    t = text.strip()
    return bool(_FILLER.match(t) or _SLASH.match(t))


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def previous_assistant_reply(row: dict, projects_dir: Path) -> str | None:
    """The last assistant text before this prompt in its session transcript."""
    session, project = row.get("sessionId"), row.get("project") or ""
    if not session:
        return None
    path = projects_dir / project.replace("/", "-") / f"{session}.jsonl"
    if not path.exists():
        return None
    prompt = (row.get("display") or "").strip()
    last_assistant = ""
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get("message") or {}
            if entry.get("type") == "user" and not entry.get("isMeta"):
                text = _text(message.get("content")).strip()
                if not text or text.startswith("<"):
                    continue
                if text.startswith(prompt[:60]):
                    return last_assistant[-MAX_CONTEXT_CHARS:] or None
                last_assistant = ""
            elif entry.get("type") == "assistant":
                text = _text(message.get("content")).strip()
                if text:
                    last_assistant = text
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", type=int, default=150, help="how many prompts to sample (default 150)")
    ap.add_argument("-o", "--out", default="exam.jsonl")
    ap.add_argument("--history", default=os.path.expanduser("~/.claude/history.jsonl"))
    ap.add_argument("--projects", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--labels", action="store_true", help="print the labels and exit")
    args = ap.parse_args()

    if args.labels:
        print("\n".join(INTENTS))
        return

    seen: set[str] = set()
    unique: list[dict] = []
    with open(args.history, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            text = (row.get("display") or "").strip()
            if text and text not in seen and not decided_by_rule(text):
                seen.add(text)
                unique.append(row)

    random.seed(args.seed)
    sample = random.sample(unique, min(args.n, len(unique)))
    projects_dir = Path(args.projects)
    with_context = 0
    with open(args.out, "w", encoding="utf-8") as out:
        for row in sample:
            context = previous_assistant_reply(row, projects_dir)
            with_context += context is not None
            out.write(
                json.dumps(
                    {
                        "text": (row.get("display") or "").strip(),
                        "project": os.path.basename(row.get("project") or "") or None,
                        "context": context,
                        "intent": "",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    print(f"{len(unique)} unique prompts, wrote {len(sample)} to {args.out} ({with_context} with context)")
    print('Now fill in "intent" on each line:', ", ".join(INTENTS))


if __name__ == "__main__":
    main()

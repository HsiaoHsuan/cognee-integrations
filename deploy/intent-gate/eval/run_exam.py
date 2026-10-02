#!/usr/bin/env python3
"""Score the gate (or the haiku baseline) against a labelled exam.

Each exam line is {"text", "project"?, "context"?, "intent": <one of 11 labels>}.
"intent" may list several acceptable labels separated by "|".

What matters most is that nothing worth keeping gets dropped, so the headline
number is recall on the "keep" class; precision comes second.

    export INTENT_GATE_URL=https://intent-gate.<subdomain>.workers.dev
    export INTENT_GATE_TOKEN=...
    python3 eval/run_exam.py eval/known_hard.jsonl
    python3 eval/run_exam.py exam.jsonl --out gate.results.jsonl
    python3 eval/run_exam.py exam.jsonl --judge haiku --out haiku.results.jsonl   # baseline
    python3 eval/run_exam.py --score gate.results.jsonl                           # re-score only
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Same client the plugin ships, so the exam scores exactly what the hook sends.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "integrations" / "claude-code" / "scripts"))
import _intent_gate as intent_gate  # noqa: E402
from _intent_gate import INTENTS, SAVE_INTENTS  # noqa: E402

DESCRIPTIONS = {
    "spec_rule": "Product rule, UI spec, or how a feature should behave",
    "preference": "Durable personal preference or standing way of working, including workflow rules such as 'after X, always do Y'",
    "decision": "A lasting choice of approach, tool, technology, name, or architecture, including one made by picking an option the assistant offered",
    "project_fact": "Project fact, constraint, or environment detail",
    "feature_req": "A feature or behavior the user wants added or changed in the product, including requests phrased as 'can you make X...' or '可以...'",
    "bug_report": "Reports that something is broken, no lasting rule",
    "command": "One-off operational instruction with no lasting product meaning (push, merge, deploy, run X, edit line N)",
    "question": "Asks for information or an explanation without requesting a product change",
    "choice_reply": "Picks among options the assistant offered when the pick is only a conversational step; if the picked option is a lasting choice (tool, name, architecture, workflow), use decision instead",
    "ack": "Acknowledgement such as ok / continue",
    "chitchat": "Small talk",
}


def load_exam(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            labels = [x.strip() for x in str(row.get("intent") or "").split("|") if x.strip()]
            if not labels:
                continue  # not labelled yet
            unknown = [x for x in labels if x not in INTENTS]
            if unknown:
                sys.exit(f"{path}:{n}: unknown intent {unknown[0]!r}")
            if len({x in SAVE_INTENTS for x in labels}) > 1:
                sys.exit(f"{path}:{n}: acceptable labels must agree on keep/drop: {row['intent']!r}")
            rows.append({**row, "labels": labels})
    return rows


# ---- judges ---------------------------------------------------------------


def judge_gate(row: dict) -> dict:
    """One exam row through the deployed gate. `raw` is Jev's own answer even when it was not trusted."""
    problems: list[str] = []
    verdict = intent_gate.judge(
        row["text"],
        context=row.get("context"),
        project=row.get("project"),
        debug=True,
        log=lambda event, detail: problems.append(f"{event} {detail}"),
    )
    debug = verdict.get("debug") or {}
    raw = debug.get("raw") or {}
    return {
        "save": verdict["save"],
        "choice": verdict["choice"],
        "confidence": verdict["confidence"],
        "source": verdict["source"],
        "raw_choice": raw.get("choice"),
        "raw_confidence": raw.get("confidence"),
        "note": debug.get("fallback_reason") or "; ".join(problems),
    }


_HAIKU_SCHEMA = {
    "type": "object",
    "properties": {"intent": {"type": "string", "enum": list(INTENTS)}, "confidence": {"type": "number"}},
    "required": ["intent", "confidence"],
}


def judge_haiku(row: dict) -> dict:
    """Baseline: Claude Haiku labels the intent through `claude -p`, same table decides keep/drop."""
    labels = "\n".join(f"- {k}: {v}" for k, v in DESCRIPTIONS.items())
    message = (
        f"Project: {row.get('project')}\nClassify the INTENT of the CURRENT user message only. "
        "Previous turn is context for short fragments; never classify the previous turn.\n"
        f"Intents:\n{labels}\n"
    )
    if row.get("context"):
        message += f"\n--- previous assistant reply (tail, context only) ---\n{row['context'][-1200:]}\n"
    message += f"\n--- CURRENT message ---\n{row['text'][:3000]}"
    try:
        proc = subprocess.run(
            ["claude", "-p", "--safe-mode", "--no-session-persistence", "--model", "haiku",
             "--output-format", "json", "--json-schema", json.dumps(_HAIKU_SCHEMA), message],
            capture_output=True, text=True, timeout=180,
            # Keeps the cognee plugin's hooks from capturing this judging call.
            env={**os.environ, "COGNEE_OBSERVER_CHILD": "1"},
        )
        if not proc.stdout.strip():
            raise ValueError(f"empty stdout (exit {proc.returncode}): {proc.stderr.strip()[:120]}")
        out = json.loads(proc.stdout)
        data = out.get("structured_output") or json.loads(out["result"])
        choice = data["intent"]
        if choice not in INTENTS:
            raise ValueError(f"unknown intent {choice!r}")
        confidence = float(data.get("confidence", 0))
    except Exception as exc:
        # Same fail-open rule as the gate: a failed judgement keeps the prompt.
        return {"save": True, "choice": "project_fact", "confidence": 0, "source": "fallback",
                "raw_choice": None, "raw_confidence": None, "note": f"{type(exc).__name__}: {exc}"[:200]}
    return {"save": choice in SAVE_INTENTS, "choice": choice, "confidence": confidence, "source": "haiku",
            "raw_choice": choice, "raw_confidence": confidence, "note": ""}


# ---- scoring --------------------------------------------------------------


def pct(a: int, b: int) -> str:
    return f"{a}/{b} ({100 * a / b:.0f}%)" if b else "n/a"


def keep_drop(results: list[dict], saved) -> dict:
    """Counts with `saved(result)` deciding what the judge kept."""
    should = [r for r in results if r["labels"][0] in SAVE_INTENTS]
    kept = [r for r in results if saved(r)]
    hit = [r for r in should if saved(r)]
    return {"should": len(should), "kept": len(kept), "hit": len(hit), "total": len(results)}


def report(results: list[dict]) -> bool:
    """Print the score card. Returns True when nothing worth keeping was dropped."""
    n = len(results)
    if not n:
        print("no labelled rows")
        return True
    k = keep_drop(results, lambda r: r["save"])
    judged = [r for r in results if r["source"] != "fallback"]
    correct = [r for r in judged if r["choice"] in r["labels"]]
    by_source = Counter(r["source"] for r in results)

    print(f"\n{n} labelled prompts  ·  decided by: " + ", ".join(f"{s} {c}" for s, c in by_source.most_common()))
    print(f"keep recall     {pct(k['hit'], k['should'])}   <- worth keeping and kept; the number to protect")
    print(f"keep precision  {pct(k['hit'], k['kept'])}   <- of what was kept, how much was worth it")
    print(f"dropped         {pct(n - k['kept'], n)}   <- noise the gate removed")
    print(f"intent accuracy {pct(len(correct), len(judged))}   (exact label, fallbacks excluded)")

    lost = [r for r in results if r["labels"][0] in SAVE_INTENTS and not r["save"]]
    if lost:
        print(f"\nLOST: {len(lost)} prompt(s) worth keeping were dropped")
        for r in lost:
            print(f"  want {r['intent']:<13} got {r['choice']:<13} conf {r['confidence']:.2f} | {one_line(r['text'])}")
    noise = [r for r in results if r["labels"][0] not in SAVE_INTENTS and r["save"]]
    if noise:
        print(f"\nkept although droppable: {len(noise)}")
        for r in noise[:15]:
            why = "fallback" if r["source"] == "fallback" else r["choice"]
            print(f"  want {r['intent']:<13} got {why:<13} conf {r['confidence']:.2f} | {one_line(r['text'])}")
        if len(noise) > 15:
            print(f"  ... and {len(noise) - 15} more")

    wrong = Counter((r["labels"][0], r["choice"]) for r in judged if r["choice"] not in r["labels"])
    if wrong:
        print("\nmost common confusions (want -> got):")
        for (want, got), c in wrong.most_common(8):
            print(f"  {c:>3}  {want} -> {got}")

    sweep = [r for r in results if r.get("raw_choice") and r.get("raw_confidence") is not None]
    if sweep:
        print(f"\nconfidence threshold sweep ({len(sweep)} prompts Jev answered; below the threshold a prompt is kept)")
        print("  threshold  keep recall   keep precision   dropped")
        for t in (0.0, 0.3, 0.5, 0.7, 0.8, 0.9):
            s = keep_drop(sweep, lambda r, t=t: r["raw_confidence"] < t or r["raw_choice"] in SAVE_INTENTS)
            print(f"  {t:>6.1f}     {pct(s['hit'], s['should']):<13} {pct(s['hit'], s['kept']):<16} {pct(len(sweep) - s['kept'], len(sweep))}")
    return not lost


def one_line(text: str, width: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("exam", nargs="?", help="labelled exam (.jsonl)")
    ap.add_argument("--judge", choices=("gate", "haiku"), default="gate")
    ap.add_argument("--out", help="write per-prompt results here (.jsonl)")
    ap.add_argument("--score", metavar="RESULTS", help="re-score a results file without calling anything")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    if args.score:
        with open(args.score, encoding="utf-8") as fh:
            results = [json.loads(line) for line in fh if line.strip()]
    else:
        if not args.exam:
            ap.error("give an exam file, or --score RESULTS")
        if args.judge == "gate" and not (os.environ.get("INTENT_GATE_URL") and os.environ.get("INTENT_GATE_TOKEN")):
            sys.exit("set INTENT_GATE_URL and INTENT_GATE_TOKEN first")
        rows = load_exam(args.exam)
        judge = judge_gate if args.judge == "gate" else judge_haiku
        with ThreadPoolExecutor(args.workers) as pool:
            results = [{**row, **verdict} for row, verdict in zip(rows, pool.map(judge, rows))]
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                for r in results:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    for r in results:
        mark = "ok  " if r["choice"] in r["labels"] else ("keep" if r["source"] == "fallback" else "MISS")
        print(f"{mark} want {r['intent']:<26} got {r['choice']:<13} {'save' if r['save'] else 'drop'} "
              f"conf {r['confidence']:.2f} {r['source']:<8} | {one_line(r['text'], 40)}"
              + (f"  [{r['note']}]" if r.get("note") else ""))
    ok = report(results)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

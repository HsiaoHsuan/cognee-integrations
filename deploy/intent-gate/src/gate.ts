import { QUESTION, buildRequest, callWithRetry, errText, unwrap, validateChoice, type JevCall, type RetryOptions } from "./jev";
import { decide, fallback } from "./policy";
import { ruleVerdict } from "./rules";
import { DROP_INTENTS, InputError, SAVE_INTENTS, type Intent, type IntentInput, type JudgeResult } from "./types";

// The whole decision in one place:
//   rule layer -> Jev -> validate -> keep-or-drop by probability mass -> intent table
// Any failure after the rule layer keeps the prompt (fail-open).
//
// Jev answers one probability per intent. The question that matters is keep or
// drop, so the decision adds the probabilities up per side instead of asking
// whether the single top intent is sure of itself: "command 0.53, chitchat
// 0.36, question 0.06" is a confident drop (0.95) even though no one intent is.

export interface GateDeps {
  callJev: JevCall;
  /** Drop only when the drop intents together reach this probability. */
  dropThreshold: number;
  /** File under user_context only when `preference` alone reaches this probability. */
  routeThreshold: number;
  /** node_set for project intents when the caller sends no project. */
  defaultProjectNodeSet: string;
  retry?: RetryOptions;
  log?: (event: string, detail: Record<string, unknown>) => void;
  now?: () => number;
}

/** Validates what a caller sent. Throws InputError; never calls Jev. */
export function parseInput(raw: unknown): IntentInput {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new InputError("body must be a JSON object");
  }
  const { text, context, project } = raw as Record<string, unknown>;
  if (typeof text !== "string" || !text.trim()) {
    throw new InputError("text must be a non-blank string");
  }
  if (context != null && typeof context !== "string") throw new InputError("context must be a string or null");
  if (project != null && typeof project !== "string") throw new InputError("project must be a string or null");
  return { text, context: context ?? null, project: project ?? null };
}

export async function judge(input: IntentInput, deps: GateDeps): Promise<JudgeResult> {
  const now = deps.now ?? Date.now;
  const started = now();
  const log = deps.log ?? (() => {});

  const ruled = ruleVerdict(input.text, input.context);
  if (ruled) {
    return { output: decide(ruled, 1, "rule", input.project, deps.defaultProjectNodeSet), debug: {} };
  }

  const failOpen = (reason: string, debug: JudgeResult["debug"] = {}): JudgeResult => {
    const result: JudgeResult = {
      output: fallback(input.project, deps.defaultProjectNodeSet),
      debug: { ...debug, fallback_reason: reason, elapsed_ms: now() - started },
    };
    log("intent_gate_fallback", { reason, ...result.debug });
    return result;
  };

  let response: unknown;
  let attempts: number;
  try {
    ({ response, attempts } = await callWithRetry(deps.callJev, buildRequest(input), deps.retry));
  } catch (err) {
    return failOpen(`jev_error: ${errText(err)}`, { attempts: (err as { attempts?: number }).attempts });
  }

  let valid;
  let model: string | undefined;
  let usage: unknown;
  try {
    const unwrapped = unwrap(response);
    model = unwrapped.model;
    usage = unwrapped.usage;
    valid = validateChoice(unwrapped.answers[QUESTION]);
  } catch (err) {
    return failOpen(`invalid_response: ${errText(err)}`, { attempts, model });
  }

  const debug = { raw: valid, model, usage, attempts };
  const p = valid.probabilities;
  const pDrop = sum(DROP_INTENTS.map((k) => p[k] ?? 0));
  const pKeep = sum(SAVE_INTENTS.map((k) => p[k] ?? 0));

  if (pDrop >= deps.dropThreshold) {
    const choice = top(DROP_INTENTS, p);
    return {
      output: decide(choice, round(pDrop), "jev", input.project, deps.defaultProjectNodeSet),
      debug: { ...debug, p_drop: round(pDrop), elapsed_ms: now() - started },
    };
  }
  // Not a confident drop. Keep it; when keeping is not even the likelier side,
  // say so (fallback) rather than present a guess as Jev's verdict.
  if (pKeep < pDrop) return failOpen("uncertain", { ...debug, p_drop: round(pDrop) });

  // A preference goes to the shared user_context only when Jev is sure of that
  // label itself; otherwise the prompt stays with the project, under the most
  // likely project intent.
  let choice = top(SAVE_INTENTS, p);
  if (choice === "preference" && (p.preference ?? 0) < deps.routeThreshold) {
    choice = top(
      SAVE_INTENTS.filter((k) => k !== "preference"),
      p,
    );
  }
  return {
    output: decide(choice, round(pKeep), "jev", input.project, deps.defaultProjectNodeSet),
    debug: { ...debug, p_drop: round(pDrop), elapsed_ms: now() - started },
  };
}

function top<T extends Intent>(keys: readonly T[], p: Record<string, number>): T {
  return keys.reduce((best, k) => ((p[k] ?? 0) > (p[best] ?? 0) ? k : best));
}

function sum(values: number[]): number {
  return values.reduce((a, b) => a + b, 0);
}

function round(x: number): number {
  return Math.round(x * 1e4) / 1e4;
}

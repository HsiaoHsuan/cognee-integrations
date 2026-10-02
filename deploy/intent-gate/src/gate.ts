import { QUESTION, buildRequest, callWithRetry, errText, unwrap, validateChoice, type JevCall, type RetryOptions } from "./jev";
import { decide, fallback, isSaveIntent } from "./policy";
import { ruleVerdict } from "./rules";
import { InputError, SAVE_INTENTS, type IntentInput, type JudgeResult } from "./types";

// The whole decision in one place:
//   rule layer -> Jev -> validate -> confidence threshold -> intent table
// Any failure after the rule layer keeps the prompt (fail-open).

export interface GateDeps {
  callJev: JevCall;
  /** Below this confidence the verdict is not trusted. */
  threshold: number;
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

  const ruled = ruleVerdict(input.text);
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
  if (valid.confidence < deps.threshold) {
    return failOpen("low_confidence", debug);
  }
  // A drop is only taken when it clearly beats every keep intent. Real Jev
  // derives confidence from the top probability, so this cannot trigger above
  // the threshold; it guards against a provider that reports them independently.
  if (!isSaveIntent(valid.choice)) {
    const top = valid.probabilities[valid.choice] ?? 0;
    if (SAVE_INTENTS.some((k) => (valid.probabilities[k] ?? 0) >= top - 1e-6)) {
      return failOpen("tie_with_keep_intent", debug);
    }
  }

  return {
    output: decide(valid.choice, valid.confidence, "jev", input.project, deps.defaultProjectNodeSet),
    debug: { ...debug, elapsed_ms: now() - started },
  };
}

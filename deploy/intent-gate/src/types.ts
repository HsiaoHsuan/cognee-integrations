// The public contract. Callers (plugins, hooks, agents) only ever see these shapes.

/** Intents that are worth keeping in long-term memory. */
export const SAVE_INTENTS = ["spec_rule", "preference", "decision", "project_fact", "feature_req"] as const;
/** Intents that are dropped. */
export const DROP_INTENTS = ["bug_report", "command", "question", "choice_reply", "ack", "chitchat"] as const;
export const INTENTS = [...SAVE_INTENTS, ...DROP_INTENTS] as const;

export type Intent = (typeof INTENTS)[number];

export interface IntentInput {
  /** The user's current prompt. Required, non-blank. */
  text: string;
  /** Tail of the previous assistant reply. Context for short fragments only. */
  context?: string | null;
  /** Project name, e.g. "ffmpeg". Becomes the node_set for project intents. */
  project?: string | null;
}

export interface IntentOutput {
  save: boolean;
  choice: Intent;
  /** 0–1. For a Jev verdict, the summed probability of the side taken (keep or
   *  drop), not of `choice` alone. Always 1 when a rule decided, 0 on fallback. */
  confidence: number;
  /** Where to store when save=true; null when save=false. */
  node_set: string | null;
  /** Who decided. "fallback" = Jev failed or was unsure, so the prompt is kept (fail-open). */
  source: "rule" | "jev" | "fallback";
}

/** Extra detail for evaluation and logs. Never part of the public contract. */
export interface JudgeDebug {
  /** Why a fallback happened, if it did. */
  fallback_reason?: string;
  /** What Jev actually answered, even when the verdict was not trusted. */
  raw?: { choice: Intent; confidence: number; probabilities: Record<string, number> };
  /** Sum of Jev's probabilities over the drop intents: what keep/drop was decided on. */
  p_drop?: number;
  model?: string;
  usage?: unknown;
  attempts?: number;
  elapsed_ms?: number;
}

export interface JudgeResult {
  output: IntentOutput;
  debug: JudgeDebug;
}

/** Thrown for caller mistakes (blank text, wrong types). Maps to HTTP 400. */
export class InputError extends Error {
  override name = "InputError";
}

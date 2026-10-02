import { SAVE_INTENTS, type Intent, type IntentOutput } from "./types";

// Intent -> action. This table lives in code, not in the Jev criteria, so the
// keep/drop policy can change without touching the prompt the model sees.

export const USER_CONTEXT_NODE_SET = "user_context";

const SAVE = new Set<Intent>(SAVE_INTENTS);

export function isSaveIntent(intent: Intent): boolean {
  return SAVE.has(intent);
}

export function projectNodeSet(project: string | null | undefined, fallback: string): string {
  const p = typeof project === "string" ? project.trim() : "";
  return p || fallback;
}

/** `save` and `node_set` are always derived from `choice`; they can never disagree. */
export function decide(
  choice: Intent,
  confidence: number,
  source: "rule" | "jev",
  project: string | null | undefined,
  defaultProjectNodeSet: string,
): IntentOutput {
  if (!isSaveIntent(choice)) {
    return { save: false, choice, confidence, node_set: null, source };
  }
  const node_set = choice === "preference" ? USER_CONTEXT_NODE_SET : projectNodeSet(project, defaultProjectNodeSet);
  return { save: true, choice, confidence, node_set, source };
}

/**
 * Fail-open verdict: when the judge is broken or unsure, keep the prompt the
 * way it was kept before the gate existed. A judge that silently drops data
 * when it fails is a failure nobody would notice.
 */
export function fallback(project: string | null | undefined, defaultProjectNodeSet: string): IntentOutput {
  return {
    save: true,
    choice: "project_fact", // placeholder, per contract
    confidence: 0,
    node_set: projectNodeSet(project, defaultProjectNodeSet),
    source: "fallback",
  };
}

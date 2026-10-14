import type { Intent } from "./types";

// Cheap, deterministic layer that runs before Jev. Returns the intent when a
// rule is certain, or null to hand the prompt to the model.

const TRAILING = String.raw`[!！。.~～ ]*`;

// Carries no information whatever came before it.
const ACK = new RegExp(String.raw`^(繼續|continue|thanks|thank you|謝謝|go|lgtm)${TRAILING}$`, "i");

// "ok" / "好" is usually filler, but right after a yes/no proposal ("要改用
// Postgres 嗎？") a bare "好" is the user accepting it, i.e. often a decision.
// So it goes to the model only when the previous reply ends with a question
// mark; anywhere else it stays an ack. Limiting it to question-ending context
// keeps the extra Jev calls rare.
const SOFT_ACK = new RegExp(String.raw`^(ok|okay|好|好的|嗯)${TRAILING}$`, "i");
const ENDS_WITH_QUESTION = /[?？]$/;

// A bare yes/no is an acknowledgement only when nothing was asked. After a
// question ("switch to Postgres?") the same "no" is a decision, so it goes to
// the model, which sees the previous reply.
const YES_NO = new RegExp(String.raw`^(yes|no|y|n|對|是|不是|要|不要|不用)${TRAILING}$`, "i");

// Only a bare slash command ("/clear", "/compact") is decided here. Anything
// with more text goes to the model, because a prompt that merely starts with
// a path ("/tmp 不要放暫存檔", "/workspaces/ffmpeg/a.png 這裡跑版") looks the same
// and a rule verdict has no fail-open behind it.
const SLASH_COMMAND = /^\/[\w:-]+$/;

export function ruleVerdict(text: string, context?: string | null): Intent | null {
  const t = text.trim();
  const ctx = context?.trim() ?? "";
  if (ACK.test(t)) return "ack";
  if (SOFT_ACK.test(t) && !ENDS_WITH_QUESTION.test(ctx)) return "ack";
  if (YES_NO.test(t) && !ctx) return "ack";
  if (SLASH_COMMAND.test(t)) return "command";
  return null;
}

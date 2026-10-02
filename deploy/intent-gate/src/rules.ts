import type { Intent } from "./types";

// Cheap, deterministic layer that runs before Jev. Returns the intent when a
// rule is certain, or null to hand the prompt to the model.

const FILLER = /^(ok|okay|好|好的|嗯|對|是|繼續|continue|yes|no|thanks|謝謝|go|y|n|lgtm)[!！。. ]*$/i;

// Only a bare slash command ("/clear", "/compact") is decided here. Anything
// with more text goes to the model, because a prompt that merely starts with
// a path ("/tmp 不要放暫存檔", "/workspaces/ffmpeg/a.png 這裡跑版") looks the same
// and a rule verdict has no fail-open behind it.
const SLASH_COMMAND = /^\/[\w:-]+$/;

export function ruleVerdict(text: string): Intent | null {
  const t = text.trim();
  if (FILLER.test(t)) return "ack";
  if (SLASH_COMMAND.test(t)) return "command";
  return null;
}

import { DurableObject } from "cloudflare:workers";
import { gateDeps, verdictTtlMs, type Env } from "./env";
import { judge } from "./gate";
import { errText } from "./jev";
import type { IntentInput, JudgeResult } from "./types";

// One instance per key (one conversation turn). It makes "whoever asks first
// triggers the judgement, everyone else gets the same answer" true no matter
// which hook or agent arrives first:
//   - a stored verdict is returned as-is
//   - a judgement already in flight is joined, not repeated
// Fallback verdicts are stored too. The first caller has already acted on
// "keep", so a later caller must not be told "drop" for the same turn.

export type CacheStatus = "miss" | "hit" | "joined";

const STORAGE_KEY = "verdict";

export class VerdictStore extends DurableObject<Env> {
  private inflight: Promise<JudgeResult> | null = null;

  async judge(input: IntentInput): Promise<JudgeResult & { cache: CacheStatus }> {
    const stored = await this.ctx.storage.get<JudgeResult>(STORAGE_KEY);
    if (stored) return { ...stored, cache: "hit" };

    if (this.inflight) return { ...(await this.inflight), cache: "joined" };

    this.inflight = (async () => {
      const result = await judge(input, gateDeps(this.env));
      try {
        // Alarm first: a stored verdict must always have its expiry scheduled.
        await this.ctx.storage.setAlarm(Date.now() + verdictTtlMs(this.env));
        await this.ctx.storage.put(STORAGE_KEY, result);
      } catch (err) {
        // Everyone waiting on this judgement still gets the same answer; only
        // callers arriving after it finishes will judge again.
        console.log(JSON.stringify({ event: "intent_gate_store_write_failed", error: errText(err) }));
      }
      return result;
    })();
    try {
      return { ...(await this.inflight), cache: "miss" };
    } finally {
      this.inflight = null;
    }
  }

  /** The verdict for this key if one exists or is being computed; otherwise null. */
  async peek(): Promise<JudgeResult | null> {
    const stored = await this.ctx.storage.get<JudgeResult>(STORAGE_KEY);
    if (stored) return stored;
    return this.inflight ? await this.inflight : null;
  }

  override async alarm(): Promise<void> {
    await this.ctx.storage.deleteAll();
  }
}

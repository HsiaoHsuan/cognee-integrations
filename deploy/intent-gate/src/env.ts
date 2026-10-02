import type { GateDeps } from "./gate";
import { cloudflareProvider, typesafeProvider } from "./jev";
import type { VerdictStore } from "./verdict-store";

export interface Env {
  AI?: { run: (model: string, input: unknown) => Promise<unknown> };
  VERDICTS: DurableObjectNamespace<VerdictStore>;

  /** Secret. Every request must present it as a Bearer token. */
  GATE_TOKEN?: string;
  /** Secret. Only for DECIDER_PROVIDER=typesafe. */
  TYPESAFE_API_KEY?: string;

  DECIDER_PROVIDER?: string;
  DECIDER_MODEL?: string;
  TYPESAFE_MODEL?: string;
  /** Override for tests; defaults to https://api.typesafe.ai */
  TYPESAFE_BASE_URL?: string;
  CONFIDENCE_THRESHOLD?: string;
  DEFAULT_PROJECT_NODE_SET?: string;
  VERDICT_TTL_SECONDS?: string;
}

export function gateDeps(env: Env): GateDeps {
  const provider = (env.DECIDER_PROVIDER || "typesafe").toLowerCase();
  const callJev =
    provider === "cloudflare" && env.AI
      ? cloudflareProvider({ ai: env.AI, model: env.DECIDER_MODEL || "typesafe/jev" })
      : typesafeProvider({
          apiKey: env.TYPESAFE_API_KEY,
          model: env.TYPESAFE_MODEL || "jev-latest",
          baseUrl: env.TYPESAFE_BASE_URL,
        });

  return {
    callJev,
    threshold: unitNumber(env.CONFIDENCE_THRESHOLD, 0.8),
    defaultProjectNodeSet: env.DEFAULT_PROJECT_NODE_SET?.trim() || "project",
    // Structured line per fallback; shows up in Workers Logs.
    log: (event, detail) => console.log(JSON.stringify({ event, ...detail })),
  };
}

export function verdictTtlMs(env: Env): number {
  const seconds = Number(env.VERDICT_TTL_SECONDS);
  return (Number.isFinite(seconds) && seconds > 0 ? seconds : 86_400) * 1000;
}

function unitNumber(raw: string | undefined, fallback: number): number {
  const n = Number(raw);
  return raw !== undefined && raw.trim() !== "" && Number.isFinite(n) && n >= 0 && n <= 1 ? n : fallback;
}

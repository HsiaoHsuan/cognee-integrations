import type { GateDeps } from "./gate";
import { JevError, cloudflareProvider, typesafeProvider, type JevCall } from "./jev";
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

export interface ProviderStatus {
  provider: string;
  model: string;
  /** Why no judgement can succeed with this configuration; null when it can. */
  problem: string | null;
}

/** What the Worker will call, and whether that can work. Never includes a secret. */
export function providerStatus(env: Env): ProviderStatus {
  const provider = (env.DECIDER_PROVIDER || "cloudflare").trim().toLowerCase();
  if (provider === "cloudflare") {
    return {
      provider,
      model: env.DECIDER_MODEL || "typesafe/jev",
      problem: env.AI ? null : "DECIDER_PROVIDER=cloudflare but the AI binding is missing",
    };
  }
  if (provider === "typesafe") {
    return {
      provider,
      model: env.TYPESAFE_MODEL || "jev-1.13.0",
      problem: env.TYPESAFE_API_KEY ? null : "DECIDER_PROVIDER=typesafe but TYPESAFE_API_KEY is not set",
    };
  }
  return { provider, model: "", problem: `unknown DECIDER_PROVIDER "${provider}" (use cloudflare or typesafe)` };
}

export function gateDeps(env: Env): GateDeps {
  const status = providerStatus(env);
  let callJev: JevCall;
  if (status.problem) {
    // Misconfigured: say so on every call instead of quietly trying another
    // provider. The verdict still fails open, and /health shows the problem.
    const problem = status.problem;
    callJev = async () => {
      throw new JevError(`misconfigured: ${problem}`, false);
    };
  } else if (status.provider === "cloudflare") {
    callJev = cloudflareProvider({ ai: env.AI!, model: status.model });
  } else {
    callJev = typesafeProvider({ apiKey: env.TYPESAFE_API_KEY, model: status.model, baseUrl: env.TYPESAFE_BASE_URL });
  }

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

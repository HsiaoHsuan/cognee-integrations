import { gateDeps, type Env } from "./env";
import { judge, parseInput } from "./gate";
import { errText } from "./jev";
import { handleMcp } from "./mcp";
import { InputError, type IntentInput, type JudgeResult } from "./types";
import type { CacheStatus } from "./verdict-store";

export { VerdictStore } from "./verdict-store";

// HTTP surface:
//   POST /v1/judge          IntentInput (+ optional `key`) -> IntentOutput
//   GET  /v1/verdict?key=   the verdict already reached for that key, or 404
//   POST /mcp               the same judgement as an MCP tool
//   GET  /health            liveness, no auth

const MAX_KEY_CHARS = 256;
const MAX_BODY_BYTES = 256 * 1024;

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/health") return Response.json({ ok: true });

    const denied = await authorize(request, env);
    if (denied) return denied;

    try {
      if (url.pathname === "/v1/judge") {
        if (request.method !== "POST") return methodNotAllowed("POST");
        const body = await readJson(request);
        const { result, cache } = await judgeShared(env, parseInput(body), parseKey(body));
        return verdictResponse(result, cache, url.searchParams.get("debug") === "1");
      }

      if (url.pathname === "/v1/verdict") {
        if (request.method !== "GET") return methodNotAllowed("GET");
        const key = parseKey({ key: url.searchParams.get("key") });
        if (!key) throw new InputError("key is required");
        const result = (await stub(env, key).peek()) as JudgeResult | null;
        if (!result) return Response.json({ error: "no verdict for this key" }, { status: 404 });
        return verdictResponse(result, "hit", url.searchParams.get("debug") === "1");
      }

      if (url.pathname === "/mcp") {
        return await handleMcp(request, async (args) => {
          const { result } = await judgeShared(env, parseInput(args), parseKey(args));
          return result.output;
        });
      }

      return Response.json({ error: "not found" }, { status: 404 });
    } catch (err) {
      if (err instanceof InputError) return Response.json({ error: err.message }, { status: 400 });
      console.log(JSON.stringify({ event: "intent_gate_error", error: errText(err) }));
      return Response.json({ error: "internal error" }, { status: 500 });
    }
  },
} satisfies ExportedHandler<Env>;

/** With a key, the turn is judged once and shared. Without one, it is judged on the spot. */
async function judgeShared(
  env: Env,
  input: IntentInput,
  key: string | null,
): Promise<{ result: JudgeResult; cache: CacheStatus | "none" }> {
  if (!key) return { result: await judge(input, gateDeps(env)), cache: "none" };
  try {
    const shared = (await stub(env, key).judge(input)) as JudgeResult & { cache: CacheStatus };
    return { result: { output: shared.output, debug: shared.debug }, cache: shared.cache };
  } catch (err) {
    // The store is an optimisation. If it is unavailable, still answer.
    console.log(JSON.stringify({ event: "intent_gate_store_error", error: errText(err) }));
    return { result: await judge(input, gateDeps(env)), cache: "none" };
  }
}

function stub(env: Env, key: string) {
  return env.VERDICTS.get(env.VERDICTS.idFromName(key));
}

function verdictResponse(result: JudgeResult, cache: string, debug: boolean): Response {
  const body = debug ? { ...result.output, debug: result.debug } : result.output;
  return Response.json(body, { headers: { "X-Intent-Gate-Cache": cache } });
}

function parseKey(body: unknown): string | null {
  const key = (body as { key?: unknown } | null)?.key;
  if (key === undefined || key === null || key === "") return null;
  if (typeof key !== "string" || key.length > MAX_KEY_CHARS) {
    throw new InputError(`key must be a string of at most ${MAX_KEY_CHARS} characters`);
  }
  return key;
}

async function readJson(request: Request): Promise<unknown> {
  const text = await request.text();
  if (text.length > MAX_BODY_BYTES) throw new InputError("body too large");
  try {
    return JSON.parse(text);
  } catch {
    throw new InputError("body must be valid JSON");
  }
}

/**
 * Fails closed: with no GATE_TOKEN configured nobody gets in, because an open
 * endpoint would let anyone spend the account's Jev budget. Callers treat a
 * refusal as "gate unavailable" and keep the prompt.
 */
async function authorize(request: Request, env: Env): Promise<Response | null> {
  if (!env.GATE_TOKEN) {
    return Response.json({ error: "GATE_TOKEN is not configured" }, { status: 503 });
  }
  const header = request.headers.get("Authorization") ?? "";
  const presented = header.startsWith("Bearer ") ? header.slice(7) : "";
  if (!(await sameSecret(presented, env.GATE_TOKEN))) {
    return Response.json({ error: "unauthorized" }, { status: 401, headers: { "WWW-Authenticate": "Bearer" } });
  }
  return null;
}

/** Compares digests so the comparison time does not depend on the secret. */
async function sameSecret(a: string, b: string): Promise<boolean> {
  const enc = new TextEncoder();
  const [da, db] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(a)),
    crypto.subtle.digest("SHA-256", enc.encode(b)),
  ]);
  const va = new Uint8Array(da);
  const vb = new Uint8Array(db);
  let diff = 0;
  for (let i = 0; i < va.length; i++) diff |= va[i]! ^ vb[i]!;
  return diff === 0;
}

function methodNotAllowed(allow: string): Response {
  return Response.json({ error: "method not allowed" }, { status: 405, headers: { Allow: allow } });
}

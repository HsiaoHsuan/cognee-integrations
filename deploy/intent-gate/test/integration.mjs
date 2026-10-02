// End-to-end check of the real Worker bundle under `wrangler dev`, with Jev
// replaced by a local stub. Covers what unit tests cannot: routing, auth, the
// Durable Object single-flight, and the MCP endpoint through a real MCP client.
//
//   node test/integration.mjs

import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const TOKEN = "test-gate-token";
const INTENTS = [
  "spec_rule", "preference", "decision", "project_fact", "feature_req",
  "bug_report", "command", "question", "choice_reply", "ack", "chitchat",
];

// ---- stub Jev -------------------------------------------------------------

const calls = []; // every request body the stub received

function stubAnswer(message) {
  let choice = "chitchat";
  if (message.includes("操作段")) choice = "spec_rule";
  else if (message.includes("merge")) choice = "command";
  else if (message.includes("B + 2")) choice = "choice_reply";
  else if (message.includes("一律")) choice = "preference";
  const p = 0.9;
  const rest = (1 - p) / (INTENTS.length - 1);
  return {
    model: "jev-stub",
    answers: {
      intent: {
        type: "choice",
        choice,
        confidence: 0.86,
        probabilities: Object.fromEntries(INTENTS.map((k) => [k, k === choice ? p : rest])),
      },
    },
    usage: { input_tokens: 100, output_tokens: 10 },
  };
}

const stub = createServer((req, res) => {
  let raw = "";
  req.on("data", (c) => (raw += c));
  req.on("end", async () => {
    const body = JSON.parse(raw);
    calls.push({ auth: req.headers.authorization, path: req.url, body });
    const message = body.state.current_message;
    if (message.includes("SLOW")) await new Promise((r) => setTimeout(r, 400));
    if (message.includes("FAIL500")) {
      res.writeHead(500).end("boom");
      return;
    }
    res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify(stubAnswer(message)));
  });
});

// ---- harness --------------------------------------------------------------

let failures = 0;
function check(name, cond, detail = "") {
  if (cond) console.log(`  ok   ${name}`);
  else {
    failures++;
    console.log(`  FAIL ${name} ${detail}`);
  }
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function startWorker(port, vars, persistDir) {
  const args = [
    "wrangler", "dev", "-c", "test/wrangler.test.jsonc",
    "--port", String(port), "--ip", "127.0.0.1", "--persist-to", persistDir,
    "--show-interactive-dev-session=false",
    ...Object.entries(vars).flatMap(([k, v]) => ["--var", `${k}:${v}`]),
  ];
  // Run wrangler's own entry point (not through npx) so the stop signal reaches it
  // and it shuts down the workerd processes it started.
  const child = spawn(process.execPath, [join(ROOT, "node_modules/wrangler/bin/wrangler.js"), ...args.slice(1)], {
    cwd: ROOT,
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, CI: "1" },
  });
  let log = "";
  child.stdout.on("data", (d) => (log += d));
  child.stderr.on("data", (d) => (log += d));
  return { child, log: () => log };
}

async function waitReady(base, worker) {
  for (let i = 0; i < 120; i++) {
    try {
      if ((await fetch(`${base}/health`)).ok) return;
    } catch {}
    if (worker.child.exitCode !== null) break;
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`worker did not start:\n${worker.log()}`);
}

function api(base, token = TOKEN) {
  return async (path, init = {}) => {
    const res = await fetch(base + path, {
      ...init,
      headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}), ...init.headers },
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
    });
    const text = await res.text();
    let json = null;
    try {
      json = JSON.parse(text);
    } catch {}
    return { status: res.status, json, cache: res.headers.get("X-Intent-Gate-Cache") };
  };
}

const callsFor = (needle) => calls.filter((c) => c.body.state.current_message.includes(needle)).length;

// ---- run ------------------------------------------------------------------

await new Promise((r) => stub.listen(0, "127.0.0.1", r));
const stubUrl = `http://127.0.0.1:${stub.address().port}`;
const persist = mkdtempSync(join(tmpdir(), "intent-gate-"));
const workers = [];

try {
  const base = "http://127.0.0.1:8799";
  const main = startWorker(8799, { GATE_TOKEN: TOKEN, TYPESAFE_API_KEY: "stub-key", TYPESAFE_BASE_URL: stubUrl }, persist);
  workers.push(main);
  await waitReady(base, main);
  const call = api(base);

  console.log("auth");
  check("/health needs no token", (await api(base, null)("/health")).status === 200);
  check("no token -> 401", (await api(base, null)("/v1/judge", { method: "POST", body: { text: "hello there" } })).status === 401);
  check("wrong token -> 401", (await api(base, "nope")("/v1/judge", { method: "POST", body: { text: "hello there" } })).status === 401);
  check("unauthorized requests never reach Jev", calls.length === 0);

  console.log("contract (examples from HANDOFF.md)");
  let r = await call("/v1/judge", { method: "POST", body: { text: "反之如果刪除段拉到其他操作段就以操作段為主", project: "ffmpeg" } });
  check("spec_rule is kept under the project", same(r.json, { save: true, choice: "spec_rule", confidence: 0.86, node_set: "ffmpeg", source: "jev" }), JSON.stringify(r.json));
  r = await call("/v1/judge", { method: "POST", body: { text: "CI 過了就 merge" } });
  check("command is dropped", same(r.json, { save: false, choice: "command", confidence: 0.86, node_set: null, source: "jev" }), JSON.stringify(r.json));
  r = await call("/v1/judge", { method: "POST", body: { text: "B + 2", context: "要 A 分頁式還是 B？匯出 1 或 2？", project: "ffmpeg" } });
  check("choice_reply is dropped", r.json?.save === false && r.json?.choice === "choice_reply", JSON.stringify(r.json));
  check("context reaches Jev as previous_assistant_reply", calls.at(-1).body.state.previous_assistant_reply === "要 A 分頁式還是 B？匯出 1 或 2？");
  check("Jev request carries model, bearer key and the 11 criteria",
    calls.at(-1).body.model === "jev-latest" && calls.at(-1).auth === "Bearer stub-key" && calls.at(-1).path === "/v1/systemone" &&
    same(Object.keys(calls.at(-1).body.questions.intent.criteria), INTENTS));
  r = await call("/v1/judge", { method: "POST", body: { text: "以後 commit 訊息一律用英文", project: "ffmpeg" } });
  check("preference goes to user_context", r.json?.save === true && r.json?.node_set === "user_context", JSON.stringify(r.json));

  console.log("rule layer");
  const before = calls.length;
  r = await call("/v1/judge", { method: "POST", body: { text: "好" } });
  check("filler decided by rule", same(r.json, { save: false, choice: "ack", confidence: 1, node_set: null, source: "rule" }), JSON.stringify(r.json));
  r = await call("/v1/judge", { method: "POST", body: { text: "/clear" } });
  check("slash command decided by rule", r.json?.source === "rule" && r.json?.choice === "command");
  check("rules do not call Jev", calls.length === before);

  console.log("caller errors");
  check("blank text -> 400", (await call("/v1/judge", { method: "POST", body: { text: "  " } })).status === 400);
  check("missing text -> 400", (await call("/v1/judge", { method: "POST", body: {} })).status === 400);
  check("GET /v1/judge -> 405", (await call("/v1/judge")).status === 405);
  check("unknown path -> 404", (await call("/nope")).status === 404);

  console.log("fail-open");
  r = await call("/v1/judge?debug=1", { method: "POST", body: { text: "FAIL500 please merge", project: "ffmpeg" } });
  check("Jev 500 -> kept as fallback", r.status === 200 && r.json?.save === true && r.json?.source === "fallback" && r.json?.confidence === 0 && r.json?.choice === "project_fact" && r.json?.node_set === "ffmpeg", JSON.stringify(r.json));
  check("debug explains the fallback", /HTTP 500/.test(r.json?.debug?.fallback_reason ?? ""));
  check("500 is not retried", callsFor("FAIL500") === 1);
  r = await call("/v1/judge", { method: "POST", body: { text: "CI 過了就 merge" } });
  check("debug stays out of normal responses", r.json && !("debug" in r.json));

  console.log("one verdict per turn (key)");
  const key = "sess-1:prompt-1";
  const burst = await Promise.all(
    Array.from({ length: 6 }, () => call("/v1/judge", { method: "POST", body: { text: "SLOW 所有的操作段如果拉超過刪除段", project: "ffmpeg", key } })),
  );
  check("6 concurrent callers -> Jev called once", callsFor("SLOW") === 1, `calls=${callsFor("SLOW")}`);
  check("all callers got the same verdict", burst.every((b) => same(b.json, burst[0].json) && b.json?.choice === "spec_rule"));
  const caches = burst.map((b) => b.cache).sort();
  check("exactly one miss, the rest joined/hit", caches.filter((c) => c === "miss").length === 1 && caches.every((c) => ["miss", "joined", "hit"].includes(c)), caches.join(","));
  r = await call("/v1/judge", { method: "POST", body: { text: "SLOW 所有的操作段如果拉超過刪除段", project: "ffmpeg", key } });
  check("later caller -> hit, still one Jev call", r.cache === "hit" && callsFor("SLOW") === 1);
  r = await call(`/v1/verdict?key=${encodeURIComponent(key)}`);
  check("GET /v1/verdict returns it without the text", r.status === 200 && same(r.json, burst[0].json));
  check("unknown key -> 404", (await call("/v1/verdict?key=nobody")).status === 404);
  check("missing key -> 400", (await call("/v1/verdict")).status === 400);
  r = await call("/v1/judge", { method: "POST", body: { text: "CI 過了就 merge", key: "sess-1:prompt-2" } });
  check("a different key is judged separately", r.cache === "miss" && r.json?.choice === "command");
  r = await call("/v1/judge", { method: "POST", body: { text: "FAIL500 again", project: "p", key: "sess-1:prompt-3" } });
  const again = await call("/v1/judge", { method: "POST", body: { text: "FAIL500 again", project: "p", key: "sess-1:prompt-3" } });
  check("a fallback verdict is shared too", r.json?.source === "fallback" && again.cache === "hit" && same(again.json, r.json));

  console.log("MCP");
  const client = new Client({ name: "integration-test", version: "0.0.0" });
  await client.connect(
    new StreamableHTTPClientTransport(new URL(`${base}/mcp`), { requestInit: { headers: { Authorization: `Bearer ${TOKEN}` } } }),
  );
  const tools = await client.listTools();
  check("lists judge_intent", tools.tools.length === 1 && tools.tools[0].name === "judge_intent");
  const res = await client.callTool({ name: "judge_intent", arguments: { text: "CI 過了就 merge", project: "joy-app" } });
  check("tool returns the IntentOutput", same(res.structuredContent, { save: false, choice: "command", confidence: 0.86, node_set: null, source: "jev" }), JSON.stringify(res.structuredContent));
  const shared = await client.callTool({ name: "judge_intent", arguments: { text: "anything", key } });
  check("tool shares verdicts by key with HTTP callers", same(shared.structuredContent, burst[0].json));
  const bad = await client.callTool({ name: "judge_intent", arguments: { text: "" } });
  check("blank text is a tool error, not a crash", bad.isError === true);
  await client.close();
  const noAuth = await fetch(`${base}/mcp`, { method: "POST", body: "{}" });
  check("MCP needs the token", noAuth.status === 401);

  console.log("no GATE_TOKEN configured");
  const closedBase = "http://127.0.0.1:8798";
  const closed = startWorker(8798, { TYPESAFE_API_KEY: "stub-key", TYPESAFE_BASE_URL: stubUrl }, mkdtempSync(join(tmpdir(), "intent-gate-")));
  workers.push(closed);
  await waitReady(closedBase, closed);
  const n = calls.length;
  r = await api(closedBase, "anything")("/v1/judge", { method: "POST", body: { text: "hello there" } });
  check("refuses everything with 503", r.status === 503 && calls.length === n);
} catch (err) {
  failures++;
  console.error(err);
} finally {
  await Promise.all(
    workers.map(
      (w) =>
        new Promise((resolve) => {
          if (w.child.exitCode !== null) return resolve();
          w.child.once("exit", resolve);
          w.child.kill("SIGTERM");
          setTimeout(resolve, 8000);
        }),
    ),
  );
  stub.close();
  rmSync(persist, { recursive: true, force: true });
}

console.log(failures ? `\n${failures} check(s) FAILED` : "\nall integration checks passed");
process.exit(failures ? 1 : 0);

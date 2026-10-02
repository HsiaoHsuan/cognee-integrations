import { INTENTS, InputError, type IntentOutput } from "./types";

// Minimal stateless MCP server (Streamable HTTP, JSON responses) exposing one
// tool. For agents that have MCP but no hooks. No sessions, no streaming: each
// POST carries one JSON-RPC message and gets one JSON reply.

const PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"];
const SERVER_INFO = { name: "intent-gate", version: "0.1.0" };

const TOOL = {
  name: "judge_intent",
  title: "Judge whether a user message is worth remembering",
  description:
    "Classifies the intent of one user message and says whether it should be kept in long-term memory and under which node_set. " +
    "Call it once per user message before storing anything. Pass the same `key` for the same conversation turn so every caller gets the same verdict.",
  inputSchema: {
    type: "object",
    properties: {
      text: { type: "string", description: "The user's current message." },
      context: { type: "string", description: "Tail of the previous assistant reply (context for short fragments)." },
      project: { type: "string", description: "Project name; becomes the node_set for project intents." },
      key: { type: "string", description: "Stable id for this turn, e.g. `<session_id>:<prompt_id>`." },
    },
    required: ["text"],
  },
  outputSchema: {
    type: "object",
    properties: {
      save: { type: "boolean" },
      choice: { type: "string", enum: [...INTENTS] },
      confidence: { type: "number" },
      node_set: { type: ["string", "null"] },
      source: { type: "string", enum: ["rule", "jev", "fallback"] },
    },
    required: ["save", "choice", "confidence", "node_set", "source"],
  },
} as const;

type JudgeFn = (args: Record<string, unknown>) => Promise<IntentOutput>;

interface RpcMessage {
  jsonrpc?: unknown;
  id?: unknown;
  method?: unknown;
  params?: unknown;
}

export async function handleMcp(request: Request, runJudge: JudgeFn): Promise<Response> {
  if (request.method !== "POST") {
    return new Response(null, { status: 405, headers: { Allow: "POST" } });
  }

  let message: unknown;
  try {
    message = await request.json();
  } catch {
    return rpcError(null, -32700, "Parse error");
  }
  if (Array.isArray(message) || typeof message !== "object" || message === null) {
    return rpcError(null, -32600, "Expected a single JSON-RPC message");
  }

  const { id, method, params } = message as RpcMessage;
  if (typeof method !== "string") return rpcError(id ?? null, -32600, "Invalid request");

  // Notifications (no id) get no body.
  if (id === undefined || id === null) return new Response(null, { status: 202 });

  switch (method) {
    case "initialize": {
      const requested = (params as { protocolVersion?: unknown } | undefined)?.protocolVersion;
      const protocolVersion =
        typeof requested === "string" && PROTOCOL_VERSIONS.includes(requested) ? requested : PROTOCOL_VERSIONS[0];
      return rpcResult(id, { protocolVersion, capabilities: { tools: {} }, serverInfo: SERVER_INFO });
    }
    case "ping":
      return rpcResult(id, {});
    case "tools/list":
      return rpcResult(id, { tools: [TOOL] });
    case "tools/call": {
      const { name, arguments: args } = (params ?? {}) as { name?: unknown; arguments?: unknown };
      if (name !== TOOL.name) return rpcError(id, -32602, `Unknown tool: ${String(name)}`);
      try {
        const output = await runJudge((args ?? {}) as Record<string, unknown>);
        return rpcResult(id, {
          content: [{ type: "text", text: JSON.stringify(output) }],
          structuredContent: output,
        });
      } catch (err) {
        if (err instanceof InputError) {
          return rpcResult(id, { content: [{ type: "text", text: err.message }], isError: true });
        }
        throw err;
      }
    }
    default:
      return rpcError(id, -32601, `Method not found: ${method}`);
  }
}

function rpcResult(id: unknown, result: unknown): Response {
  return Response.json({ jsonrpc: "2.0", id, result });
}

function rpcError(id: unknown, code: number, message: string): Response {
  return Response.json({ jsonrpc: "2.0", id, error: { code, message } });
}

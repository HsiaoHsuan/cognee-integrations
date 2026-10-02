import { describe, expect, it, vi } from "vitest";
import { judge, parseInput, type GateDeps } from "../src/gate";
import { JevError, buildRequest, callWithRetry, typesafeProvider, unwrap, validateChoice, type JevCall } from "../src/jev";
import { ruleVerdict } from "../src/rules";
import { INTENTS, InputError, type Intent } from "../src/types";

/** A well-formed Jev answer with `choice` as the winner at probability `p`. */
function answer(choice: Intent, p = 0.9, confidence = 0.86) {
  const rest = (1 - p) / (INTENTS.length - 1);
  const probabilities = Object.fromEntries(INTENTS.map((k) => [k, k === choice ? p : rest]));
  return { type: "choice", choice, probabilities, confidence };
}

function jevResponse(choice: Intent, p?: number, confidence?: number) {
  return { model: "jev-test", answers: { intent: answer(choice, p, confidence) }, usage: { input_tokens: 1 } };
}

function deps(callJev: JevCall, extra: Partial<GateDeps> = {}): GateDeps {
  return {
    callJev,
    threshold: 0.5,
    defaultProjectNodeSet: "project",
    retry: { sleep: async () => {} },
    ...extra,
  };
}

describe("rule layer", () => {
  it.each(["好", "繼續", "ok", "OK!", "好的。", "lgtm", "謝謝"])("treats %s as an acknowledgement", (t) => {
    expect(ruleVerdict(t)).toBe("ack");
  });

  it.each(["/clear", "/compact", "/cognee:recap", " /clear "])("treats the bare command %s as a slash command", (t) => {
    expect(ruleVerdict(t)).toBe("command");
  });

  it.each([
    "/workspaces/ffmpeg/截圖.png 紅色那個區域縮短",
    "/tmp 不要放暫存檔，一律用 scratchpad",
    "/src 底下一律用 TypeScript strict",
    "/README.md",
    "/model opus",
  ])("leaves %s to the model: it may be a path, not a command", (t) => {
    expect(ruleVerdict(t)).toBeNull();
  });

  it.each(["B + 2", "CI 過了就 merge", "好，但是匯出改成底部彈出", "手機版的"])("hands %s to the model", (t) => {
    expect(ruleVerdict(t)).toBeNull();
  });

  it("decides without calling Jev", async () => {
    const callJev = vi.fn();
    const { output } = await judge({ text: "好" }, deps(callJev));
    expect(output).toEqual({ save: false, choice: "ack", confidence: 1, node_set: null, source: "rule" });
    expect(callJev).not.toHaveBeenCalled();
  });
});

describe("input validation", () => {
  it.each([[null], ["text"], [[]], [{}], [{ text: "" }], [{ text: "   " }], [{ text: 42 }]])(
    "rejects %j as a caller error",
    (raw) => {
      expect(() => parseInput(raw)).toThrow(InputError);
    },
  );

  it("rejects non-string context and project", () => {
    expect(() => parseInput({ text: "x", context: 1 })).toThrow(InputError);
    expect(() => parseInput({ text: "x", project: {} })).toThrow(InputError);
  });

  it("accepts text alone", () => {
    expect(parseInput({ text: "hi there" })).toEqual({ text: "hi there", context: null, project: null });
  });
});

describe("request sent to Jev", () => {
  it("maps the input onto the three state fields", () => {
    const req = buildRequest({ text: "反之如果刪除段拉到其他操作段就以操作段為主", project: "ffmpeg" });
    expect(req.state).toEqual({
      project: "ffmpeg",
      previous_assistant_reply: null,
      current_message: "反之如果刪除段拉到其他操作段就以操作段為主",
    });
    expect(Object.keys(req.questions.intent!.criteria)).toEqual([...INTENTS]);
    expect(req.questions.intent!.type).toBe("choice");
  });

  it("keeps the tail of the context and the head of the text", () => {
    const req = buildRequest({ text: "a".repeat(5000), context: "x".repeat(2000) + "TAIL" });
    expect(req.state.current_message).toHaveLength(4000);
    expect(req.state.previous_assistant_reply).toHaveLength(1200);
    expect(req.state.previous_assistant_reply!.endsWith("TAIL")).toBe(true);
  });
});

describe("intent table", () => {
  it("stores project intents under the project", async () => {
    const { output } = await judge(
      { text: "反之如果刪除段拉到其他操作段就以操作段為主", project: "ffmpeg" },
      deps(async () => jevResponse("spec_rule")),
    );
    expect(output).toEqual({ save: true, choice: "spec_rule", confidence: 0.86, node_set: "ffmpeg", source: "jev" });
  });

  it("stores preferences under user_context regardless of project", async () => {
    const { output } = await judge(
      { text: "以後 commit 訊息一律用英文", project: "ffmpeg" },
      deps(async () => jevResponse("preference")),
    );
    expect(output.save).toBe(true);
    expect(output.node_set).toBe("user_context");
  });

  it("uses the default node_set when no project is given", async () => {
    const { output } = await judge({ text: "API 都要過 /api/v2 前綴" }, deps(async () => jevResponse("project_fact")));
    expect(output.node_set).toBe("project");
  });

  it.each(["bug_report", "command", "question", "choice_reply", "ack", "chitchat"] as const)("drops %s", async (intent) => {
    const { output } = await judge({ text: "CI 過了就 merge", project: "joy-app" }, deps(async () => jevResponse(intent)));
    expect(output).toMatchObject({ save: false, choice: intent, node_set: null, source: "jev" });
  });

  it.each(INTENTS)("keeps save and node_set consistent for %s", async (intent) => {
    const { output } = await judge({ text: "some message", project: "p" }, deps(async () => jevResponse(intent)));
    expect(output.save).toBe(output.node_set !== null);
  });
});

describe("fail-open", () => {
  const FALLBACK = { save: true, choice: "project_fact", confidence: 0, node_set: "ffmpeg", source: "fallback" };

  it("keeps the prompt when Jev fails", async () => {
    const log = vi.fn();
    const { output, debug } = await judge(
      { text: "CI 過了就 merge", project: "ffmpeg" },
      deps(
        async () => {
          throw new JevError("HTTP 500", false);
        },
        { log },
      ),
    );
    expect(output).toEqual(FALLBACK);
    expect(debug.fallback_reason).toContain("HTTP 500");
    expect(log).toHaveBeenCalledWith("intent_gate_fallback", expect.objectContaining({ reason: expect.any(String) }));
  });

  it("keeps the prompt when the response fails validation", async () => {
    const bad = jevResponse("command");
    (bad.answers.intent as { choice: string }).choice = "made_up_label";
    const { output, debug } = await judge({ text: "CI 過了就 merge", project: "ffmpeg" }, deps(async () => bad));
    expect(output).toEqual(FALLBACK);
    expect(debug.fallback_reason).toMatch(/^invalid_response/);
  });

  it("keeps the prompt when confidence is below the threshold, even for a drop intent", async () => {
    const { output, debug } = await judge(
      { text: "CI 過了就 merge", project: "ffmpeg" },
      deps(async () => jevResponse("command", 0.4, 0.34)),
    );
    expect(output).toEqual(FALLBACK);
    expect(debug.fallback_reason).toBe("low_confidence");
    expect(debug.raw?.choice).toBe("command"); // still visible for threshold tuning
  });

  it("keeps the prompt when a drop intent only ties with a keep intent", async () => {
    const tied = jevResponse("question", 0.5, 0.9);
    const probabilities = Object.fromEntries(INTENTS.map((k) => [k, k === "question" || k === "spec_rule" ? 0.5 : 0]));
    (tied.answers.intent as { probabilities: Record<string, number> }).probabilities = probabilities;
    const { output, debug } = await judge({ text: "這個要怎麼處理比較好", project: "ffmpeg" }, deps(async () => tied));
    expect(output).toEqual(FALLBACK);
    expect(debug.fallback_reason).toBe("tie_with_keep_intent");
  });

  it("trusts a verdict exactly at the threshold", async () => {
    const { output } = await judge({ text: "CI 過了就 merge" }, deps(async () => jevResponse("command", 0.6, 0.5)));
    expect(output.source).toBe("jev");
    expect(output.save).toBe(false);
  });
});

describe("response validation", () => {
  const good = () => answer("command");

  it("accepts a well-formed answer", () => {
    expect(validateChoice(good()).choice).toBe("command");
  });

  it("rejects a choice outside the criteria", () => {
    expect(() => validateChoice({ ...good(), choice: "other" })).toThrow(/not one of the criteria/);
  });

  it("rejects missing or extra probability keys", () => {
    const missing = good();
    delete (missing.probabilities as Record<string, number>).ack;
    expect(() => validateChoice(missing)).toThrow(/keys do not match/);

    const extra = good();
    (extra.probabilities as Record<string, number>).other = 0;
    expect(() => validateChoice(extra)).toThrow(/keys do not match/);
  });

  it("rejects out-of-range or non-finite numbers", () => {
    const a = good();
    (a.probabilities as Record<string, number>).ack = -0.1;
    expect(() => validateChoice(a)).toThrow(/0–1/);
    expect(() => validateChoice({ ...good(), confidence: 1.2 })).toThrow(/confidence/);
    expect(() => validateChoice({ ...good(), confidence: Number.NaN })).toThrow(/confidence/);
  });

  it("rejects probabilities that do not sum to 1", () => {
    const a = good();
    (a.probabilities as Record<string, number>).command = 0.5;
    expect(() => validateChoice(a)).toThrow(/sum to/);
  });

  it("rejects a choice that is not the most probable option", () => {
    const a = good();
    a.choice = "ack";
    expect(() => validateChoice(a)).toThrow(/most probable/);
  });
});

describe("provider wrappers", () => {
  it("unwraps the direct API shape", () => {
    expect(unwrap(jevResponse("ack")).answers.intent).toBeDefined();
  });

  it("unwraps the Workers AI REST envelope", () => {
    const wrapped = { success: true, result: { state: "Completed", result: jevResponse("ack") } };
    expect(unwrap(wrapped).model).toBe("jev-test");
  });

  it("treats success=false or an unfinished state as failure", () => {
    expect(() => unwrap({ success: false, errors: [] })).toThrow(JevError);
    expect(() => unwrap({ success: true, result: { state: "Running" } })).toThrow(/state is Running/);
    expect(() => unwrap({ nothing: true })).toThrow(/no answers/);
  });

  it("sends the model and bearer token to the TypeSafe API", async () => {
    const fetchImpl = vi.fn(async () => Response.json(jevResponse("ack")));
    const call = typesafeProvider({ apiKey: "k", model: "jev-latest", fetchImpl: fetchImpl as unknown as typeof fetch });
    await call(buildRequest({ text: "hello there" }), new AbortController().signal);
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://api.typesafe.ai/v1/systemone");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer k");
    expect(JSON.parse(init.body as string)).toMatchObject({ model: "jev-latest", state: { current_message: "hello there" } });
  });
});

describe("retry and timeout", () => {
  const body = buildRequest({ text: "hello there" });

  it("retries 429/503/529 with 500ms × 2^n backoff, then succeeds", async () => {
    const sleeps: number[] = [];
    const call = vi
      .fn<JevCall>()
      .mockRejectedValueOnce(new JevError("HTTP 429", true))
      .mockRejectedValueOnce(new JevError("HTTP 529", true))
      .mockResolvedValueOnce(jevResponse("ack"));
    const { attempts } = await callWithRetry(call, body, { sleep: async (ms) => void sleeps.push(ms) });
    expect(attempts).toBe(3);
    expect(sleeps).toEqual([500, 1000]);
  });

  it("gives up after 3 retries", async () => {
    const call = vi.fn<JevCall>().mockRejectedValue(new JevError("HTTP 503", true));
    await expect(callWithRetry(call, body, { sleep: async () => {} })).rejects.toThrow("HTTP 503");
    expect(call).toHaveBeenCalledTimes(4);
  });

  it("does not retry other failures", async () => {
    const call = vi.fn<JevCall>().mockRejectedValue(new JevError("HTTP 401", false));
    await expect(callWithRetry(call, body, { sleep: async () => {} })).rejects.toThrow("HTTP 401");
    expect(call).toHaveBeenCalledTimes(1);
  });

  it("marks only 429/503/529 as retryable", async () => {
    for (const [status, retryable] of [[429, true], [503, true], [529, true], [500, false], [422, false]] as const) {
      const call = typesafeProvider({
        apiKey: "k",
        model: "m",
        fetchImpl: (async () => new Response("x", { status })) as unknown as typeof fetch,
      });
      const err = await call(body, new AbortController().signal).catch((e) => e);
      expect(err).toBeInstanceOf(JevError);
      expect((err as JevError).retryable).toBe(retryable);
    }
  });

  it("times out a call that never returns", async () => {
    const call: JevCall = () => new Promise(() => {});
    await expect(callWithRetry(call, body, { timeoutMs: 20 })).rejects.toThrow(/timed out/);
  });
});

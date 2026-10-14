# intent-gate

判斷一句使用者 prompt 值不值得進長期記憶，以及該存到哪個 `node_set`。
規格來自 `HANDOFF.md`（Intent Gate：用 TypeSafe JEV 判斷 prompt 意圖），做成一個多個 hook／agent 可以共用的 Cloudflare Worker。

```
prompt ─▶ 規則層 ─▶ Jev（只回 11 個固定標籤 + 機率）─▶ 驗證 ─▶ 「丟」類機率加總 ─▶ 查表 ─▶ 存／丟 + node_set
           │                         └──────── 任何失敗或沒把握 ────────▶ 照舊存（fail-open）
           └─ 繼續／謝謝／lgtm、單獨一個 /指令：直接判，不呼叫 Jev
              好／ok／嗯：前一輪回覆以問號（? 或 ？）結尾時交給 Jev（可能是在接受提議），其他情況直接判 ack
              是／不是／no／不要：只有沒有前一輪回覆時才直接判；有的話交給 Jev（可能是在回答問題）
```

Jev 的 11 個 criteria 在 `src/criteria.json`，照 TypeSafe 文件建議的結構化寫法：每個選項是一個物件，`what`（是什麼）、`not_for`（容易混淆時該改選哪個鄰近選項）、`examples`（1–3 個例句）。Worker 直接 import 這個檔，`eval/run_exam.py` 的 haiku 基準也讀同一份，不另外抄一份。

## 介面

所有端點（`/health` 除外）都要 `Authorization: Bearer $GATE_TOKEN`。

| 端點 | 用途 |
|---|---|
| `POST /v1/judge` | 判斷一句 prompt |
| `GET /v1/verdict?key=` | 拿同一輪已經判好的結果（沒有就 404），不用再送原文 |
| `POST /mcp` | 同一個判斷包成 MCP tool `judge_intent`，給有 MCP 但沒有 hook 的 agent |
| `GET /health` | 存活檢查和目前設定（provider、model、`ready`、`problem`，不含密鑰），不用 token |

```jsonc
// POST /v1/judge
{ "text": "反之如果刪除段拉到其他操作段就以操作段為主",   // 必填
  "context": null,      // 選填：前一輪 assistant 回覆尾段（只取最後 1200 字）
  "project": "ffmpeg",  // 選填：專案名，專案類意圖的 node_set
  "key": "sess:prompt"  // 選填：同一輪共用判斷用，見下
}
// →
{ "save": true, "choice": "spec_rule", "confidence": 0.94, "node_set": "ffmpeg", "source": "jev" }
```

- `save`、`node_set` 一律由 `choice` 查表推出，三者不會互相矛盾。
- `source`：`rule`（規則層）、`jev`、`fallback`（Jev 失敗，或「存」「丟」兩邊都不夠確定，照舊存：`save=true`、`choice="project_fact"`、`confidence=0`）。
- `confidence`：Jev 判斷時是**所選那一邊**（存或丟）的機率加總，不是 `choice` 單一標籤的機率；規則層判的是 1，fallback 是 0。

### 怎麼決定存或丟

Jev 對 11 個標籤各給一個機率。真正要決定的是「存還是丟」，所以把兩邊各自加總，而不是只看最高的那一個標籤夠不夠確定：

1. 6 個「丟」類標籤加總 ≥ `DROP_THRESHOLD`（預設 0.85）→ 丟，`choice` 是其中機率最高的。
2. 否則，如果「存」類加總比「丟」類小 → 兩邊都不確定，照舊存（`fallback`，`debug.fallback_reason="uncertain"`）。
3. 否則存，`choice` 是「存」類中機率最高的；只有 `preference` 自己 ≥ `ROUTE_THRESHOLD`（預設 0.6）才放 `user_context`，不然改用次高的專案類標籤，放專案。

例：`command 0.53、chitchat 0.36、question 0.06` —— 沒有任何單一標籤超過 0.8，但「丟」類加總 0.95，所以丟。舊版（只看最高標籤 ≥ 0.8）會把這句照舊存。

`?debug=1` 的 `debug.p_drop` 是「丟」類加總，`debug.raw.probabilities` 是 Jev 原始的 11 個機率。
- `text` 空白或不是字串 → `400`，不呼叫 Jev。
- 加 `?debug=1` 會多回一個 `debug` 欄位（Jev 原始的 choice／機率、fallback 原因、耗時），給考卷和除錯用。

| intent | 動作 | node_set |
|---|---|---|
| `preference` | 存 | `user_context` |
| `spec_rule` `decision` `project_fact` `feature_req` | 存 | `project`（沒給就用 `DEFAULT_PROJECT_NODE_SET`） |
| `bug_report` `command` `question` `choice_reply` `ack` `chitchat` | 丟 | — |

這張表在 `src/policy.ts`，不在 Jev 的 criteria 裡，改策略不用動 prompt。

## 同一輪只判一次（`key`）

Claude Code 同一個事件的 hook 是平行跑的，沒有先後順序，hook 之間也不能傳資料。所以不排順序，改成「誰先問都一樣」：

- 每個 hook 的輸入都有 `session_id` 和 `prompt_id`，用 `session_id:prompt_id` 當 `key`，不同 plugin 不用互相溝通就能指到同一輪。
- 同一個 `key`：第一個請求觸發 Jev，同時到的請求等同一個結果，之後到的直接拿存好的結果。
- fallback 的結果也會共用（第一個呼叫者已經照「存」處理了，後面的不能拿到「丟」）。
- 結果存 `VERDICT_TTL_SECONDS`（預設 24 小時）後自動清掉。只存判斷結果，不存 prompt 原文。

回應的 `X-Intent-Gate-Cache` header 會是 `miss`／`joined`／`hit`／`none`。

## 設定

`wrangler.jsonc` 的 vars：

| 變數 | 預設 | 說明 |
|---|---|---|
| `DECIDER_PROVIDER` | `cloudflare` | `cloudflare`＝Workers AI binding（不用 key，Unified Billing、不在免費額度）；`typesafe`＝直接打 `api.typesafe.ai` |
| `DECIDER_MODEL` | `typesafe/jev` | Workers AI 的模型 ID。Workers AI 目前沒有可固定版本的 ID，模型更新時判斷結果可能會變；`?debug=1` 回應裡的 `model` 會記錄實際用的版本 |
| `TYPESAFE_MODEL` | `jev-1.13.0` | `typesafe` provider 用。固定版本，不用 `jev-latest`：門檻是用這一版測出來的，換版前先重跑考卷 |
| `DROP_THRESHOLD` | `0.85` | 「丟」類 6 個標籤的機率加總到這個值才丟；不到就存 |
| `ROUTE_THRESHOLD` | `0.6` | `preference` 自己的機率到這個值才放 `user_context`；不到就放專案 |
| `DEFAULT_PROJECT_NODE_SET` | `project` | 呼叫端沒給 `project` 時用 |
| `VERDICT_TTL_SECONDS` | `86400` | 帶 key 的判斷保留多久 |

Secrets：

```sh
openssl rand -hex 32 | npx wrangler secret put GATE_TOKEN   # 必要；沒設時所有請求回 503
npx wrangler secret put TYPESAFE_API_KEY                    # 只有 DECIDER_PROVIDER=typesafe 才需要
```

設定錯誤（`DECIDER_PROVIDER=cloudflare` 卻沒有 AI binding、`typesafe` 卻沒有 key、或 provider 名稱打錯）不會改走另一個 provider：每次判斷都記一筆 `misconfigured` 的 fallback 並照舊存，`/health` 的 `problem` 會寫出原因。

逾時：每次呼叫 Jev 最多 10 秒、只重試 1 次（429／503／529），整個判斷最多 20 秒，比 plugin 預設的 30 秒短。這樣 plugin 放棄等待後，Worker 不會再為同一輪寫入不同的判斷。

## 部署

```sh
npm install
npx wrangler deploy
```

## 接到 cognee-memory（Claude Code plugin）

plugin 端的改動已經在這個 fork 的 `integrations/claude-code/`（版本 `1.6.x-gate.*`），不是另外的 patch。安裝：`/plugin marketplace add HsiaoHsuan/cognee-integrations` → `/plugin install cognee-memory@cognee`。不要改 `~/.claude/plugins/cache/…`（plugin 更新會蓋掉）。

plugin 測試：

```sh
cd integrations/tests && COGNEE_TEST_SUITE=claude-code uv run pytest tests/unit/test_intent_gate_patch.py tests/unit/test_user_agent.py
```

plugin 端的環境變數：

| 變數 | 說明 |
|---|---|
| `COGNEE_CAPTURE_JUDGE` | `true` 才啟用（預設關，行為跟沒 patch 一樣） |
| `INTENT_GATE_URL` | Worker 網址 |
| `INTENT_GATE_TOKEN` | 跟 Worker 的 `GATE_TOKEN` 同一串 |
| `INTENT_GATE_TIMEOUT` | 等判斷的秒數，預設 30 |

plugin 端做的事：

1. `store-user-prompt.py`：**先**照舊把 prompt 暫存，**再**問 gate，把判斷結果附在暫存的那筆上。Stop 如果比判斷早到，這一輪就照舊存。
2. `store-to-session.py`（Stop）：判斷是「丟」就整筆 QA 不存（問題和回答都不存）；`question`、`bug_report` 例外，整輪照存，因為 gate 只看 prompt，這兩種的價值在回答（找到的原因、修法）。判斷是 `preference` 就把這筆的 node_set 改成 `user_context`。判斷結果綁定 `session_id:prompt_id`，跟這次 Stop 對不上（例如上一輪被中斷留下來的）就忽略，照舊存。
3. `_plugin_common.py`：暫存的 prompt 帶著判斷結果走；entry 自帶的 node_set 只有在這個 session 已經啟用並驗證過 project node set 時才會送出。
4. `_intent_gate.py`：呼叫這個 Worker 的 client，只用標準函式庫，任何錯誤都回「照舊存」。`eval/` 和 `test/py/` 直接 import 它，只有這一份。
5. `_ua.py`：plugin 所有 urllib 請求改送 `cognee-memory-plugin/<版本>`（`workers.dev` 會用 1010 擋預設的 `Python-urllib`）。

已知限制：

- 需要 hook 輸入裡有 `prompt_id`（Claude Code v2.1.196 以上）。沒有的話判斷結果無法對到那一輪，一律照舊存。
- PostToolUse 的 trace（工具呼叫紀錄）不受 gate 影響，照舊存。
- 暫存檔的讀寫沒有上鎖（上游原本就是這樣）。判斷結果寫回時如果剛好撞上 Stop 取走或下一輪寫入，可能留下一筆過期的暫存；因為有上面的綁定，過期的判斷不會被套用。

## 驗收（考卷）

```sh
export INTENT_GATE_URL=https://intent-gate.<subdomain>.workers.dev
export INTENT_GATE_TOKEN=...

python3 eval/run_exam.py eval/known_hard.jsonl          # 已知難題（含上下文裡的是非題、客氣的指令），先確認通
python3 eval/run_exam.py eval/phrasings.jsonl           # 64 句：11 種意圖各用多種說法（直述、疑問、條件、否定、客氣、英文、短句、帶路徑）

python3 eval/make_exam.py -n 150 -o exam.jsonl          # 從 ~/.claude/history.jsonl 抽樣，含前一輪回覆
#   → 手動把每行的 "intent" 填上 11 個標籤之一（可用 a|b 表示都算對）
python3 eval/run_exam.py exam.jsonl --out gate.results.jsonl
python3 eval/run_exam.py exam.jsonl --judge haiku --out haiku.results.jsonl   # 基準線
```

報表的重點是 **keep recall**（該存的有沒有被存到），其次才是 precision；`LOST` 清單列出每一句被誤丟的；最後有 `DROP_THRESHOLD` 掃描：用每句 Jev 原始的「丟」類加總，在本機重算不同門檻下的結果，不用再呼叫 Jev。
`exam.jsonl` 和結果檔是你真實的 prompt，不要 commit 到公開 repo。

## 跟原始規格（HANDOFF，未收錄）不一樣的地方

| HANDOFF | 這裡 | 原因 |
|---|---|---|
| plugin 直接呼叫 Jev | plugin 呼叫這個 Worker，Worker 呼叫 Jev | 多個 hook／agent 共用同一個判斷和同一份 Jev 金鑰；`DECIDER_*`、`TYPESAFE_*` 設在 Worker 上 |
| `/` 開頭一律當指令丟掉 | 只有整句就是一個 `/名稱`（如 `/clear`）才由規則層判；後面還有字的交給 Jev | `/tmp 不要放暫存檔`、`/workspaces/ffmpeg/a.png 這裡跑版` 這種以路徑開頭的 prompt 長得跟指令一樣，而規則層判錯沒有 fail-open 可以救 |
| 規則層擋空白 | 空白回 `400` | HANDOFF 的「約束」一節說空白是呼叫端錯誤要 raise，兩處寫法不同，取較嚴格的 |
| 信心低於門檻 → 照舊存 | 看「丟」類機率加總（`DROP_THRESHOLD`），不看單一標籤的信心；兩邊都不確定時照舊存，輸出統一成 fallback 形狀 | 單一標籤的信心會在「指令／閒聊」這種兩個都該丟的標籤之間被分掉，結果該丟的被存下來（線上 2026-10-14 的三筆 fallback 都是這樣） |
| 插入點在 `remember_pending_prompt` 之前，丟就不暫存 | 先暫存、再判斷，Stop 時才決定存不存 | 上游的 Stop hook 找不到暫存的 prompt 時，仍會把回答存成一筆「問題是空字串」的 QA；而且判斷變慢時 Stop 可能先到 |
| 重試「最多 3 次」 | 最多重試 3 次（共 4 次嘗試），間隔 500／1000／2000ms | 照字面 |

## 測試

```sh
npm run check   # typecheck + 單元測試 + wrangler dev 整合測試（Jev 用本機假伺服器）+ Python 測試
```

- `test/gate.test.ts`：規則層、請求格式、驗證、查表、fail-open、重試與逾時
- `test/integration.mjs`：真的 Worker bundle 跑在 `wrangler dev`，含認證、Durable Object 的同輪共用、用官方 MCP client 連 `/mcp`
- `test/py/`：Python client 和考卷工具
- plugin 端的測試在 `integrations/tests/tests/unit/test_intent_gate_patch.py`、`test_user_agent.py`

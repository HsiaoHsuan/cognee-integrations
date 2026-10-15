---
name: cognee-search
description: Search the user's long-term Cognee memory — decisions, standing preferences, product rules, project facts and bug fixes kept from earlier sessions. Use it whenever the answer may depend on something from before this conversation and not visible in it or in the code — "like last time", "what did we decide about X", "why is it done this way", a convention or constraint you are unsure of, or a bug that looks familiar. Search instead of guessing.
---

# Cognee Memory Search

Search the permanent knowledge graph, optionally filtered by node set.

## When to search

Search when the answer may hinge on what earlier sessions settled and this conversation does not show:

- the user points back in time ("like last time", "as we decided", "the usual way")
- you are about to choose an approach, name, tool or layout the project may already have decided
- a constraint or convention might exist that the code does not make obvious
- an error looks like one that was fixed before

Do not search for things the conversation or the code already answers. One broad query is usually enough.

If a `UserPromptSubmit` memory block is already in the conversation (`COGNEE_RECALL_MODE=auto`; the default `skill` injects none), search only when it is not enough.

## What is stored

Each distilled memory is one sentence with its reason, filed as a lesson of the session it came from (`# Session learning (session <id>)`), under node sets you can filter by:

| Node set | Contains |
|----------|----------|
| `project-<name>-<hash>` | this project's memories (also `decision`, `spec_rule`, `project_fact`, `feature_req`, `bug_fix`) |
| `user_context` | the user's standing preferences (also `preference`), and what `cognee-remember` stores for the user |
| `session_learnings` | every distilled lesson; `session_learnings:<session id>` for one session's |

## Data categories

Knowledge is organized into three categories via `node_set`:

| Category | Node set | Contains |
|----------|----------|----------|
| **user** | `user_context` | User preferences, corrections, personal facts |
| **project** | `project_docs` | Repository docs, code context, architecture decisions |
| **agent** | `agent_actions` | Tool call logs, reasoning traces, generated artifacts |

## Rules

- **Server first.** Search goes through the **running Cognee server**
  (`POST /api/v1/recall`) via the wrapper below — the source of truth in both
  local and cloud mode.
- **`cognee-cli` is a last resort, not an alternative.** It is reachable only on a
  machine holding a cognee **source checkout**, and in cloud mode the plugin's
  venv is never built at all. Use it only when the server is genuinely
  unreachable *and* that checkout exists. Unsure which mode you are in?
  `"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-doctor.sh" --json` reports `mode`,
  `server_url` and `reachable` without importing cognee.
- **Empty CLI output is never proof of absence.** Ground-truth against the server
  before concluding anything.

## Instructions

Use the wrapper below: it queries the server, scoped to the **plugin's dataset** (`$COGNEE_PLUGIN_DATASET`, default `agent_sessions` — the same dataset all plugin writes target, so unrelated datasets don't bleed in), and resolves the endpoint, session and API key the way the hooks do.

**One broad search is usually enough** — avoid running many targeted searches (each is an extra permission prompt for the user).

### Search (server-first)

```bash
# permanent knowledge graph (the default; --graph is the same)
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-search.sh" "$ARGUMENTS" 10

# deterministic code graph (indexed repos only — see the cognee-code skill)
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-search.sh" "MyClass" 10 --code
```

**Structural code questions belong to `--code`, not here.** "What calls X",
"what breaks if I change X", "list all endpoints" are answered exactly and
token-free by the code graph — see the **cognee-code** skill for the
operations and for indexing a repository. Use this skill's semantic search for
conceptual questions that name no symbol ("how does auth work here?").

### Filter by category (optional)

Categories (`user_context` / `project_docs` / `agent_actions`) filter by node set. The wrapper does not expose this — pass `node_name` to the server directly.

**Resolve credentials first.** In local mode `$COGNEE_API_KEY` is empty (the key is minted into `~/.cognee-plugin/api_key.json` and never exported to your shell), so a hand-rolled `curl` with `-H "X-Api-Key: ${COGNEE_API_KEY:-}"` 401s and looks like a server fault when nothing is wrong. Use the forget helper's resolver in the **same** shell invocation — exports do not persist across separate Bash calls:

```bash
eval "$("${CLAUDE_PLUGIN_ROOT}/scripts/cognee-forget.sh" env)" && \
curl -s -X POST "${COGNEE_BASE_URL}/api/v1/recall" \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: ${COGNEE_API_KEY}" \
  -d '{"query": "...", "top_k": 5, "only_context": true, "scope": ["graph"], "node_name": ["project_docs"], "datasets": ["'"${COGNEE_PLUGIN_DATASET:-agent_sessions}"'"]}'
```

### Ground-truth a suspicious result (debugging)

The server is authoritative. If a search returns empty but you expect content, confirm directly — **do not** conclude "not found" from empty CLI output. Resolve credentials in the same invocation, as above:

```bash
eval "$("${CLAUDE_PLUGIN_ROOT}/scripts/cognee-forget.sh" env)" && \
curl -s -X POST "${COGNEE_BASE_URL}/api/v1/recall" \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: ${COGNEE_API_KEY}" \
  -d '{"query": "...", "top_k": 5, "only_context": true, "scope": ["graph"], "datasets": ["'"${COGNEE_PLUGIN_DATASET:-agent_sessions}"'"]}'
```

(The server enforces auth in both modes, which is why the resolver is not optional. If the response is an `{"error": ...}` object rather than a list, the server was reachable but rejected/failed the request — that's an error, **not** "no results". A `401` *after* the resolver means the key really is wrong for that server; a `401` without it means nothing.)

### Fallback only — server unreachable

`cognee-cli` is a thin client over the same server and can print **empty stdout even when content exists**. It also requires a cognee source checkout, so on a normal install it is simply unavailable — an absent CLI is not a Cognee fault to report. Use it only when the server is down *and* a checkout exists, and treat empty output as *inconclusive*, never as "no results":

```bash
cognee-cli recall "$ARGUMENTS" -k 5 -f json
```

If the CLI is missing, say the server is unreachable and show the user
`"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-doctor.sh"` output instead.

## Not found in the active dataset? Offer another one

Search is scoped to this session's **active** dataset. When the server answers
with an authoritative empty list (or the `UserPromptSubmit` header injected an
"Other Cognee datasets you can search" block), the information may simply live
in another dataset the user can read. Do **not** switch datasets for that — a
switch retires the session. Offer a one-off search instead:

1. List the candidates (every dataset this identity can read, the active one
   excluded; read-only datasets are searchable and included):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/list-datasets.py" --others
   ```

   The JSON has `current` (`{name, id, ids}`) and `datasets`
   (`[{name, id, owner_id, current}]`). The hook's hint block already names all
   of them with their ids, so when it is present you can skip this call.

2. Ask the user which dataset to search with **AskUserQuestion** (single
   select, dataset names as options; more than four → first three plus a
   "More…" option and page on the next question). Include a "None, stop here"
   reading in the question text. Only ask when the user is actually trying to
   recall something — an ordinary prompt with no memory match needs no picker.

3. Run the graph-only search on the chosen dataset **by UUID** (a name only
   resolves among datasets this identity owns), then answer from it and say
   which dataset the results came from:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/cognee-search.sh" "$ARGUMENTS" 10 --graph --dataset-id <id>
   ```

   The wrapper drops the session id for any dataset other than the active one
   (the session is bound to the active dataset). The active dataset, the Cognee session and where
   writes go are untouched. If the user then wants that dataset for the rest
   of the session, point them to `/cognee-memory:cognee-switch-datasets`.

## Understanding results

Results carry a `source` field:
- `"graph"` — from the permanent knowledge graph. On cognee 1.6.0 and later the
  item's `text` is the full prompt cognee would have answered from: this
  session's history, the question with the retrieved context, and the guidance
  block.
- `"code"` — deterministic facts from an indexed repository's code graph

The session cache is never a search source; it reaches the graph through the sync.

## Decision table

| Signal | Action |
|--------|--------|
| Need current session context | It is in the conversation; no search needed |
| Answer may depend on an earlier session's decision, preference, constraint or fix | `cognee-search.sh "<query>" 10` |
| User explicitly says "search cognee" | `cognee-search.sh "<query>"` (server-first) |
| "what does the codebase do" / "what did we do last time" | `cognee-search.sh "<query>" 10 --graph` |
| Need a specific category | use the `node_name` curl form above (`["user_context"\|"project_docs"\|"agent_actions"]`) |
| Auto context insufficient (`COGNEE_RECALL_MODE=auto`) | `cognee-search.sh "<query>" 10` (memory is graph + code only; the session cache is written, never searched) |
| **Server says empty and the user is recalling something** | **Offer the other readable datasets (`list-datasets.py --others` + AskUserQuestion), then `cognee-search.sh "<query>" 10 --graph --dataset-id <id>`** |
| **Result empty but you expect content** | **Ground-truth via the credential-resolving `curl` above before concluding "not found"** |

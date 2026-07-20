# Agent K — Winning Plan for "Agents of SigNoz" Hackathon

## Context

**Hackathon:** "Agents of SigNoz" (WeMakeDevs × SigNoz), **July 20–26, 2026**. Theme: *"If you can't observe your AI agents, you don't own them."* Every project must use self-hosted SigNoz (OTel-native). Submission = public repo + blog writeup (Medium/Dev.to/Substack) + demo video + official form.

**Prizes:** Track 1 "AI & Agent Observability" = MacBook Air (top prize, our target). Early Blog prize (AirPods Pro, deadline **July 19** — before hackathon) for self-hosting SigNoz + blogging about a favorite feature. Social track = swag for tagged posts. Top bloggers get SigNoz job interviews.

**Judging criteria (6):** Potential Impact · Creativity & Innovation · Technical Excellence · Best Use of SigNoz · User Experience · Presentation Quality.

**Key intel from the judges' own project board** (github.com/orgs/SigNoz/projects/65 — 25 idea issues, scraped):
- Column 1 (highest billing) is **"Agent-native Observability"**: *SRE Sidekick built on SigNoz* (#11656), *Debug production issues with SigNoz MCP* (#11654), *Self-healing infra* (#11653), *Deploy guardian* (#11657 — "correlate CI/CD deploy markers with error/latency regressions and auto-trigger rollback or Slack alert"), *Observability Slackbot* (#11655).
- Issues #11674–11678 are an explicit rubric of **Query Builder v5 capabilities SigNoz wants showcased**: complex boolean search with parentheses/`NOT EXISTS`/JSON body paths/`hasAll` on array attrs/regex secret-leak detection; aggregations `sumIf(order.total, has_error=true)`, `rate()`, `count_distinct(user.id)`; cross-context Group By (`service.name` + `http.route`), rollout comparison by `service.version`; `Having`; `Order By + Limit` top-N.
- #11658 *LLM cost tracer*: per-prompt/user/model cost dashboard + budget-spike alert.

**Decisions made with user:** Build the **AI SRE Sidekick** (Track 1) · also go for the **Early Blog prize** · run everything on **local Docker Compose** · **solo** participant (+ Claude Code as coding agent).

**Winning thesis:** One project that fuses four of the judges' own board ideas (SRE Sidekick + MCP debugging + Deploy Guardian + Self-healing) and embeds their Query Builder rubric as the agent's investigation playbook — then closes the loop with **meta-observability**: the agent that debugs your systems through SigNoz is *itself* fully OTel-instrumented into the same SigNoz (gen_ai semconv, cost tracing, "Watch the Watcher" dashboard). SigNoz observes the agent that observes SigNoz. Name: **Agent K** (hackathon has a Men-in-Black theme — "Agents of SigNoz", MIB disclaimer on the site).

---

## The Product

**Agent K** — an autonomous on-call SRE agent. A SigNoz alert fires → webhook wakes Agent K → it investigates through the **SigNoz MCP server** (traces, logs, metrics, Query Builder v5) → correlates the regression with the deploy that caused it → posts a full RCA to Slack with evidence deep-links and business blast radius ("$4,312 of orders failed, 87 users affected") → proposes a guarded remediation (rollback/flag-off) executed on human approval → and every step of its own reasoning lands in SigNoz as traces with token/cost accounting.

### Architecture (all in one `docker compose up`)

```
┌─────────────── incident sandbox ────────────────┐
│ shop demo: gateway → checkout → payment →       │
│ inventory (FastAPI ×4) + Postgres + Redis       │
│ + loadgen (steady traffic) + chaos CLI          │
└───────────────┬─────────────────────────────────┘
                │ OTLP
        ┌───────▼────────┐   alert webhook   ┌──────────────┐
        │     SigNoz     │──────────────────▶│   AGENT K    │
        │ (self-hosted)  │◀──────────────────│ FastAPI +    │
        └───────▲────────┘  MCP / API v5     │ Claude agent │
                │ OTLP (agent's own traces,  │ loop         │
                │ gen_ai.* spans, cost)      └──────┬───────┘
                └───────────────────────────────────┘
                                             Slack (RCA + approval)
```

### Components

**1. Incident sandbox — `sandbox/`** (custom 4-service shop, NOT the heavyweight OTel demo)
- FastAPI services: `gateway`, `checkout`, `payment`, `inventory` + Postgres + Redis. OTel Python SDK auto-instrumentation + manual spans.
- Resource/span attributes **deliberately designed to light up the judges' QB rubric**: `service.version`, `deployment.environment`, `order.total`, `user.id`, `tenant.name`, `feature_flags` (array attr), structured JSON log bodies with `retry_count`.
- `loadgen/`: async Python traffic generator (realistic mix, multiple tenants/users, tunable RPS).
- **Chaos CLI** (`make incident-<name>`), deterministic scenarios for demo:
  - `bad-deploy` — bumps checkout to `service.version=1.4.2` with latency+error regression, emits a deployment-marker log event (Deploy Guardian story)
  - `pool-exhaustion` — payment DB connection pool starves
  - `flag-combo` — errors only when `hasAll(feature_flags, ['new-checkout','express-pay'])`
  - `secret-leak` — AWS-key-shaped string leaks into prod logs (regex detection query)
  - `resolve` — rolls back / heals

**2. Agent core — `agent/`**
- Python 3.12, FastAPI. Endpoints: `POST /webhook/signoz` (Alertmanager-style alert payload — **verify exact schema against a live fired alert on day 1**), `POST /investigate` (manual trigger), `GET /approve/{action_id}` (one-click remediation approval from Slack).
- LLM agent loop over an **OpenAI-compatible endpoint** (`openai` SDK + `OPENAI_BASE_URL`/`OPENAI_API_KEY`/`OPENAI_MODEL` — works with OpenAI, OpenRouter, Groq, local vLLM, etc.). Tools = **SigNoz MCP server** (official `SigNoz/signoz-mcp-server`, run in compose in HTTP mode, `SIGNOZ_URL`/`SIGNOZ_API_KEY`, no OAuth). Key tools: `signoz_search_traces`, `signoz_aggregate_traces`, `signoz_get_trace_details`, `signoz_search_logs`, `signoz_aggregate_logs`, `signoz_query_metrics`, `signoz_list_alerts`, `signoz_get_alert_history`, `signoz_execute_builder_query` (raw QB v5), `signoz_create_dashboard`. **Fallback if MCP integration stalls: thin wrapper tools over SigNoz REST `query_range` v5 — same playbook, keep MCP branding in at least one path.**
- **Investigation playbook** (system prompt encodes a real SRE runbook, each step mapping to a rubric query):
  1. Triage alert → affected service/signal
  2. Top-N failing services/endpoints (`Order By + Limit`)
  3. Error characterization (complex boolean search, per-service failure criteria)
  4. **Deploy correlation**: group `p99(duration_nano)` + `countIf(has_error=true)` by `service.version`; find deployment-marker event nearest regression onset
  5. Exemplar traces (`signoz_get_trace_details`) → pinpoint failing span
  6. Logs drill-down incl. JSON-body predicates; check missing telemetry via `NOT EXISTS`
  7. **Blast radius in business terms**: `sumIf(order.total, has_error=true)`, `count_distinct(user.id)`, per-tenant Group By, `Having` to cut noise
  8. Verdict: root cause + confidence + evidence links + remediation proposal
- **RCA report**: Markdown — timeline, root cause, evidence bullets each deep-linked to SigNoz (trace URLs, pre-filtered explorer links), blast radius, suggested fix. Posted to Slack (incoming webhook + Block Kit), saved to `reports/`, served at `GET /reports` (styled HTML list — cheap UX win).
- **Guarded remediation** (self-healing story): allow-listed actions only — `rollback(service)` (compose image-tag swap), `disable_flag(flag)`, `restart(service)`. Agent proposes; Slack message contains Approve link → agent executes → posts verification (re-runs the regression query, confirms recovery). `AUTO_APPROVE=true` env for the demo video's dramatic finish.

**3. Meta-observability — the differentiator**
- Agent fully OTel-instrumented into the **same SigNoz**: one trace per investigation; every LLM call a span with **OTel gen_ai semconv** (`gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`) + computed `llm.cost.usd`; every MCP tool call a child span with query + result size.
- **"Watch the Watcher" dashboard**: investigations over time, time-to-RCA, tokens & $ per investigation (per-model, per-incident — folds in the *LLM cost tracer* board idea), tool error rate, LLM latency p95.
- **Alerts on the agent itself**: cost-per-hour budget spike, investigation-failure alert. If Agent K's budget alert fires… it investigates itself. (One-liner for the demo/blog.)

**4. Dashboards & alerts as code — `provisioning/`**
- JSON dashboard + alert-rule definitions applied via SigNoz API/MCP by `make provision`: (a) shop service dashboards, (b) Watch-the-Watcher, (c) LLM cost. Alert rules: checkout error-rate, p99 latency, secret-leak log match, agent budget. Reproducible = Technical Excellence points.

### Repo layout

```
agent-k/
├─ docker-compose.yml          # SigNoz + sandbox + mcp-server + agent + loadgen
├─ Makefile                    # up / provision / incident-* / demo / down
├─ sandbox/{gateway,checkout,payment,inventory,loadgen,chaos}/
├─ agent/{main.py, loop.py, playbook.py, tools_mcp.py, remediation.py,
│         telemetry.py (gen_ai spans + cost), report.py, slack.py}
├─ provisioning/{dashboards/*.json, alerts/*.json, provision.py}
├─ reports/                    # generated RCAs (committed samples)
├─ docs/{architecture.png, demo-script.md}
└─ README.md                   # hero gif, quickstart ≤3 commands, arch diagram
```

---

## Schedule (solo + Claude Code)

**Pre-hackathon (Jul 13–19) — Early Blog prize + prep (no project code before Jul 20):**
- Jul 13–14: Self-host SigNoz via Docker on the Mac (≥8 GB to Docker). Instrument a toy app, explore hands-on.
- Jul 15–17: Write + publish blog on Dev.to/Medium: *favorite feature = Query Builder v5 search expressions* (genuine walkthrough with own screenshots; their issue examples show what excites them — write from real usage, not AI filler; low-effort/AI content explicitly loses). Tag @wemakedevs + SigNoz on the social post (swag track).
- Jul 18–19: Submit blog form. Dry-run risky unknowns *as throwaway experiments*: fire a test alert → capture real webhook JSON; run MCP server in HTTP mode and hit it from a script; skim SigNoz QB v5 API payload shape.

**Hackathon week (Jul 20–26):**
- **D1 Jul 20** — Repo scaffold, compose stack green: SigNoz + 4 shop services instrumented + loadgen. Traces/logs visible in SigNoz.
- **D2 Jul 21** — Chaos scenarios (all 4, deterministic) + `provisioning/` dashboards & alert rules. Alert → webhook payload received by stub endpoint.
- **D3 Jul 22** — Agent loop + MCP tools end-to-end: alert fires → agent investigates → first RCA lands in Slack. (Biggest technical risk day; fallback to REST wrapper tools if MCP fights back.)
- **D4 Jul 23** — Deploy correlation + playbook depth (all rubric queries exercised) + RCA quality + remediation w/ approval + verification loop.
- **D5 Jul 24** — Meta-observability: gen_ai spans, cost computation, Watch-the-Watcher dashboard, agent budget alert. `/reports` HTML page.
- **D6 Jul 25** — Polish + presentation: README w/ hero GIF + arch diagram, `make demo` one-shot, **record 3–4 min demo video** (script: incident fires → phone buzzes → Agent K's RCA in Slack → approve rollback → recovery graph → cut to Watch-the-Watcher dashboard: "and here's what it cost: $0.23"), screenshots.
- **D7 Jul 26** — Submission blog (separate from early blog: problem → build story → SigNoz features used: traces/logs/metrics/dashboards/alerts/MCP/QB — name them explicitly per submission requirements → challenges → lessons), submit form, social posts. Buffer.

## Judging-criteria mapping (keep visible in README + blog)

| Criterion | Answer |
|---|---|
| Impact | Real on-call pain; MTTR cut from alert→RCA in minutes; business-value blast radius |
| Creativity | Closed loop: agent debugs via SigNoz *and* is observed by SigNoz; alert-triggered autonomy; self-investigating budget alert |
| Technical excellence | Full OTel semconv (incl. gen_ai), dashboards/alerts-as-code, deterministic chaos harness, guarded remediation w/ verification |
| Best use of SigNoz | All signals + dashboards + alerts + **MCP server** + QB v5 rubric queries (their own issues #11674–78) as the playbook |
| UX | Slack-native RCA w/ deep links + one-click approve; `/reports` page; 3-command quickstart |
| Presentation | Scripted demo video, hero GIF, arch diagram, quality blog |

## Risks & mitigations
- **SigNoz + stack RAM on laptop** → trim ClickHouse resources, 4 slim services, loadgen at low RPS.
- **MCP server integration friction** → REST `query_range` wrapper fallback (D3 decision point).
- **Webhook schema assumptions** → captured real payload during prep week.
- **LLM cost** → pick a cheap capable model on the OpenAI-compatible endpoint, cap max tool iterations, truncate tool results.
- **Non-deterministic demo** → chaos scenarios are scripted and idempotent; rehearse `make demo` before recording.

## Verification
- `make up && make provision && make incident-bad-deploy` → within ~2 min: SigNoz alert fires → Slack RCA identifying `checkout v1.4.2` with trace links + blast radius → approve → rollback → recovery confirmation message.
- Each chaos scenario produces a correct-root-cause RCA (spot-check all 4).
- Agent K's own investigation trace visible in SigNoz with gen_ai token/cost attrs; Watch-the-Watcher dashboard populated; budget alert fires under a forced token-burn test.
- Fresh-clone test on clean machine/VM: README quickstart alone reproduces the demo.

---
---

# BUILD SPECIFICATION (for the coding agent)

Architecture decisions below are **final** — do not re-litigate them. Where a detail says *(verify)*, check the named source at implementation time and adapt mechanically; everything else, build as written. Work through Appendix H's checkpoints in order; each checkpoint must pass before starting the next.

## Appendix A — Tech stack (locked, with rationale)

| Layer | Choice | Why |
|---|---|---|
| Language (everything) | **Python 3.12** | One language across sandbox + agent + chaos + provisioning. Best OTel auto-instrumentation maturity and native `gen_ai` semconv support. `openai` SDK first-class. |
| Package manager | **uv** (one `pyproject.toml` per service dir, workspace root) | Fast, lockfiles, reproducible builds in Docker. |
| Web framework | **FastAPI + uvicorn** | Auto-instrumented by `opentelemetry-instrumentation-fastapi`; pydantic v2 models for webhook payloads. |
| LLM | **OpenAI-compatible endpoint** via `openai` Python SDK: `OPENAI_BASE_URL` + `OPENAI_API_KEY` + `OPENAI_MODEL` env — works with OpenAI, OpenRouter, Groq, local vLLM/Ollama, etc. Tool use via chat-completions function calling. | Provider-agnostic; user's requirement. Loop quality is playbook-driven, not model-limited. |
| Agent↔SigNoz | **Official `SigNoz/signoz-mcp-server`** (Go binary, run in compose, `TRANSPORT_MODE=http`, no OAuth) via `mcp` Python SDK streamable-HTTP client | Judges' board explicitly features MCP; server exposes 50+ tools incl. `signoz_execute_builder_query`. |
| Fallback Agent↔SigNoz | `tools_rest.py` wrappers over SigNoz REST (query_range v5) | Only if MCP client integration burns >½ day (D3 decision point). Same tool names/signatures so `loop.py` doesn't change. |
| Telemetry | `opentelemetry-sdk`, `opentelemetry-exporter-otlp` (gRPC→collector :4317), instrumentation pkgs: fastapi, httpx, psycopg, redis | |
| Data stores (sandbox) | Postgres 16, Redis 7 | Realistic dependencies; Redis doubles as the chaos-flag store. |
| Agent state | **SQLite** (stdlib `sqlite3`, WAL mode), file at `/data/agentk.db` volume | Zero-ops; investigations + actions tables only. |
| Slack | **Incoming webhook** for posts; approval = HMAC-signed link back to agent (`GET /approve/...`) | Avoids building a full Slack app (no socket mode, no OAuth). |
| Lint/format | ruff (format+lint), pytest | Keep CI-less; `make check` runs both. |

**Considered and rejected:** *Effect TS / TypeScript stack* — Effect's typed effects buy long-term robustness we don't need in a 7-day build, TS OTel `gen_ai` semconv support is less mature than Python's, and it would split the codebase into two languages (sandbox realism favors Python). *OTel Demo (Astronomy Shop)* — 20+ services, RAM-heavy, no control over attributes; our 4-service shop is designed to light up the QB rubric. *LangChain/LangGraph* — unnecessary abstraction; a hand-rolled loop over OpenAI function calling is smaller, fully traceable, and reads better in the blog.

## Appendix B — Runtime topology (docker compose)

Two compose layers, one network:
- `deploy/signoz/` — the **official SigNoz self-host compose** (clickhouse, signoz, signoz-otel-collector). Obtain per current self-host docs *(verify: https://signoz.io/docs/install/docker/)*. Pin the version. Reduce ClickHouse memory limits for laptop use.
- `docker-compose.yml` (repo root) — our services, joined to the SigNoz compose network (`external: true`).

| Service | Build | Port (host) | Key env |
|---|---|---|---|
| `signoz` | official | 8080 UI | — |
| `signoz-otel-collector` | official | 4317/4318 OTLP | — |
| `mcp-server` | `SigNoz/signoz-mcp-server` (build from source or release binary in slim image) | 8000 | `TRANSPORT_MODE=http`, `SIGNOZ_URL=http://signoz:8080`, `SIGNOZ_API_KEY` |
| `gateway` | `sandbox/gateway` | 8001 | `OTEL_SERVICE_NAME=gateway`, common OTEL_* below |
| `checkout` | `sandbox/checkout` | 8002 | `SERVICE_VERSION` (default `1.4.1`), `CHAOS_MODE` (default empty) |
| `payment` | `sandbox/payment` | 8003 | `PG_DSN`, `POOL_SIZE=10` |
| `inventory` | `sandbox/inventory` | 8004 | `REDIS_URL` |
| `postgres` | postgres:16-alpine | — | |
| `redis` | redis:7-alpine | — | |
| `loadgen` | `sandbox/loadgen` | — | `TARGET=http://gateway:8001`, `RPS=5` |
| `agent` | `agent/` | 9000 | see Appendix D env table |

Common OTel env on every sandbox service + agent:
`OTEL_EXPORTER_OTLP_ENDPOINT=http://signoz-otel-collector:4317`, `OTEL_RESOURCE_ATTRIBUTES=deployment.environment=production,service.version=${SERVICE_VERSION}`.

SigNoz API key: created manually once in SigNoz UI after first boot (Settings → API Keys), stored in `.env` (git-ignored; `.env.example` committed). README documents this as step 2 of quickstart.

## Appendix C — Sandbox specification

### C1. Service behavior ("AstroMart" mini-shop)

- **gateway** — `POST /api/checkout` (body: generated by loadgen: `{user_id, tenant, items[], total}`) → forwards to checkout. `GET /api/products` → inventory. Adds span attr `tenant.name`.
- **checkout** — `POST /checkout`: sets span attrs `order.total` (float), `user.id`, `tenant.name`, `feature_flags` (**array** attr via `span.set_attribute("feature_flags", ["new-checkout", ...])`). Calls `payment /pay` then `inventory /reserve`. Emits structured JSON log per order: `{"event":"order_processed","order_id":...,"retry_count":N,"consumer_group":"orders-cg", ...}`.
- **payment** — `POST /pay`: Postgres INSERT into `payments` through a pool of `POOL_SIZE`; ~30–80 ms simulated work.
- **inventory** — `GET /products`, `POST /reserve`: Redis reads/writes.
- All services: manual root-span enrichment middleware + auto-instrumentation; logs via Python `logging` bridged to OTLP (`opentelemetry-sdk` logs API), JSON bodies.

### C2. Load generator
Async httpx loop, `RPS` env, weighted mix: 70% checkout, 30% products. Rotates through 20 fake users, 3 tenants (`M&M toys`, `acme`, `globex`), order totals $5–$500, and randomly assigns `feature_flags` from {`new-checkout`, `express-pay`, `gift-wrap`} (each order 0–2 flags). Deterministic seed for reproducibility.

### C3. Chaos engine
Chaos flags live in **Redis** (`chaos:<name>` = "1"); services check the flag per-request (cheap GET, cached 1 s). `sandbox/chaos/chaos.py` CLI (`python -m chaos <scenario>|resolve`) sets/clears flags. Scenarios:

| Scenario | Mechanism | Observable symptom |
|---|---|---|
| `bad-deploy` | Restart checkout with `SERVICE_VERSION=1.4.2 CHAOS_MODE=bad-deploy` (chaos.py shells out to `docker compose up -d checkout` with env). In this mode checkout adds +800 ms latency and fails 35% of orders (HTTP 500, `has_error=true`). chaos.py also emits a one-shot **deployment marker log** via OTLP: `{"event":"deployment","service":"checkout","version":"1.4.2"}` from `service.name=deploy-bot`. | p99 + error-rate step change, correlated to version 1.4.2 marker |
| `pool-exhaustion` | Flag makes payment hold connections 5 s → pool starves → timeouts | payment latency spike, timeout errors, checkout cascades |
| `flag-combo` | Checkout fails (25%) **only when** order has BOTH `new-checkout` AND `express-pay` | only `hasAll(feature_flags, [...])` isolates it |
| `secret-leak` | Payment logs a fake `AKIA...`-pattern key + fake JWT in an error log body (prod env) | regex log alert fires |
| `resolve` | Clears flags; for bad-deploy restarts checkout at 1.4.1 + emits rollback marker | recovery visible |

Each scenario has a paired **alert rule** (Appendix E) so the demo is always: `make incident-X` → alert → Agent K.

## Appendix D — Agent K specification

### D1. Env/config (`agent/config.py`, pydantic-settings)
`OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`, `LLM_INPUT_PRICE_PER_MTOK` + `LLM_OUTPUT_PRICE_PER_MTOK` (cost accounting is provider-dependent, so prices come from env; default 0 → cost panels show tokens only), `SIGNOZ_URL` (browser-facing, e.g. `http://localhost:8080`, for deep links), `MCP_URL=http://mcp-server:8000`, `SLACK_WEBHOOK_URL`, `AGENT_PUBLIC_URL=http://localhost:9000`, `APPROVAL_SECRET` (HMAC), `AUTO_APPROVE=false`, `MAX_ITERATIONS=20`, `MAX_COST_USD_PER_INVESTIGATION=1.00`, `DB_PATH=/data/agentk.db`.

### D2. HTTP surface (`agent/main.py`)
- `POST /webhook/signoz` — SigNoz alert webhook. Payload is Alertmanager-style (`{alerts:[{status,labels,annotations,startsAt,...}]}`) — *(verify against the real payload captured in prep week; store the sample as `agent/tests/fixtures/webhook_sample.json` and code the pydantic model from it)*. Dedupe: skip if an investigation for the same `alertname` is already `running`. Fires background task → `run_investigation(trigger)`.
- `POST /investigate` — `{"prompt": "checkout latency looks bad"}` manual trigger.
- `GET /approve/{action_id}?sig=<hmac>` — verifies `HMAC(APPROVAL_SECRET, action_id)`, executes the pending action, returns a small HTML confirmation, posts verification result to Slack.
- `GET /reports` + `GET /reports/{id}` — HTML list/detail of RCAs (markdown rendered server-side, dark monospace styling; keep to one Jinja template).
- `GET /healthz`.

### D3. State (`agent/store.py`, SQLite)
```sql
CREATE TABLE investigations(id TEXT PK, trigger_json TEXT, status TEXT, -- running|done|failed
  started_at TEXT, finished_at TEXT, report_md TEXT, root_cause TEXT,
  cost_usd REAL, tokens_in INT, tokens_out INT, trace_id TEXT);
CREATE TABLE actions(id TEXT PK, investigation_id TEXT, kind TEXT, params_json TEXT,
  status TEXT, -- proposed|approved|executed|verified|rejected
  created_at TEXT, executed_at TEXT, verification_md TEXT);
```

### D4. Agent loop (`agent/loop.py`)
Hand-rolled tool-use loop over OpenAI chat-completions function calling (`openai.OpenAI(base_url=OPENAI_BASE_URL)`):
```
messages = [system: PLAYBOOK, user: render_trigger(trigger)]
for i in range(MAX_ITERATIONS):
    resp = client.chat.completions.create(model, tools=TOOLS, messages)   # wrapped in llm.call span
    if resp.choices[0].message.tool_calls:
        for call in tool_calls:
            if call.name == "finish_investigation": persist + notify; return
            if call.name == "propose_remediation": create action row + Slack approve button; append result
            else: result = mcp.call(call.name, call.args)      # wrapped in tool.<name> span
            append tool_result (truncate to 15k chars)
    running_cost += cost(resp.usage); if running_cost > MAX_COST: force finish with partial report
```
**Tools exposed to the model (curated 12, not all 50):** `signoz_list_services`, `signoz_aggregate_traces`, `signoz_search_traces`, `signoz_get_trace_details`, `signoz_search_logs`, `signoz_aggregate_logs`, `signoz_query_metrics`, `signoz_execute_builder_query`, `signoz_get_alert`, `signoz_get_alert_history` + synthetic `propose_remediation(kind, service, reason)` and `finish_investigation(report_markdown, root_cause_oneliner, confidence)`. Tool JSON schemas: pull from MCP `list_tools` at startup, filter to the allowlist, map MCP schema → OpenAI function-calling `{"type":"function","function":{name,description,parameters}}` format.

### D5. Playbook system prompt (`agent/playbook.py`)
Encodes the runbook; key clauses to write verbatim into the prompt:
1. Role: senior SRE; investigate, don't guess; every claim needs a query result behind it.
2. Ordered steps: triage alert → top-N failing services/endpoints (order by desc, limit) → characterize errors (boolean search incl. per-service failure criteria) → **deploy correlation**: group p99+countIf(has_error) by `service.version`, look for deployment-marker logs from `deploy-bot` near onset → exemplar trace deep-dive → logs (incl. JSON-body predicates) → **blast radius**: `sumIf(order.total, has_error = true)`, `count_distinct(user.id)`, per-tenant group-by with `having` to cut noise.
3. Every evidence bullet must carry a deep link: `{SIGNOZ_URL}/trace/{trace_id}` and explorer links (report.py provides `link_trace()` / `link_explorer()` helpers the model is told about; the model outputs `[evidence](signoz://trace/<id>)` placeholders that report.py rewrites — keeps URLs correct).
4. Remediation only via `propose_remediation`, only kinds `rollback|disable_flag|restart`, must state expected effect + verification query.
5. Finish via `finish_investigation` with the report template (D6). Confidence: high/medium/low. Time budget: prefer ≤12 tool calls.

### D6. Report template (`agent/report.py`)
```
# 🕶️ Agent K — Incident Report: {title}
**Status:** {root_cause_oneliner} · Confidence: {confidence}
## Timeline  (alert fired / regression onset / deploy marker / investigation span)
## Root cause  (paragraph + the one decisive piece of evidence)
## Evidence  (bullets, each with SigNoz deep link)
## Blast radius  (💸 ${order_value} failed orders · 👤 {n} users · tenants: …)
## Remediation  ({proposed action + status})
## Cost of this investigation  ({tokens} tokens · ${cost} · {duration}s · [view my trace]({link}))
```
The self-referential last line doubles as demo material.

### D7. Slack (`agent/slack.py`)
Incoming-webhook Block Kit: header block (🕶️ + root cause), fields (service, version, blast radius), evidence section (top 3 links), actions section rendered as link buttons: ✅ Approve rollback → `{AGENT_PUBLIC_URL}/approve/{action_id}?sig=…`. After execution: threaded-style follow-up message with verification result (webhooks can't thread — send a second message referencing the incident id).

### D8. Remediation (`agent/remediation.py`)
Registry dict, each entry: `run()` (subprocess `docker compose …` — agent container mounts the docker socket and the compose file read-only; document the security tradeoff in README) + `verify()` (re-run the regression QB query via MCP after 60 s, compare error rate before/after) :
- `rollback(service)` → `SERVICE_VERSION=1.4.1 CHAOS_MODE= docker compose up -d {service}` + rollback marker log
- `disable_flag(flag)` → Redis `chaos:` clear (maps to flag-combo scenario)
- `restart(service)` → `docker compose restart {service}`
`AUTO_APPROVE=true` skips the approval wait (demo mode).

### D9. Self-telemetry (`agent/telemetry.py`)
- Root span `investigation` (attrs: `agentk.trigger_type`, `agentk.alertname`, `agentk.root_cause`, `agentk.confidence`, `agentk.total_cost_usd`).
- Child span per LLM call `llm.call` with **gen_ai semconv**: `gen_ai.system=openai` (or hostname of `OPENAI_BASE_URL`), `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, plus `llm.cost.usd` computed from `LLM_INPUT_PRICE_PER_MTOK`/`LLM_OUTPUT_PRICE_PER_MTOK` env.
- Child span per tool call `tool.{name}` (attrs: args summary, result bytes, `error=true` on failure).
- Also emit **metrics**: counter `agentk.investigations`, histogram `agentk.investigation.duration`, counter `agentk.cost.usd` — simplifies the Watch-the-Watcher dashboard vs deriving everything from spans.
- `OTEL_SERVICE_NAME=agent-k`, same collector. The investigation's own `trace_id` is stored and linked in the report footer.

## Appendix E — Provisioning (`provisioning/provision.py`)
Idempotent script (create-or-update by name) against SigNoz API — use MCP tools (`signoz_create_dashboard`, `signoz_create_alert`, `signoz_create_notification_channel`) or REST equivalents *(verify: whichever is less friction on D2; dashboard JSON schema — export one hand-made dashboard from the UI first and use it as the template skeleton)*.

**Notification channels:** (1) webhook → `http://agent:9000/webhook/signoz`; (2) Slack → `SLACK_WEBHOOK_URL`.

**Alert rules (all route to both channels):**
| Rule | Query (QB v5) | Threshold |
|---|---|---|
| checkout-error-rate | traces: `countIf(has_error=true)/count()` filter `service.name='checkout'` | >10% for 1 min |
| checkout-p99 | traces: `p99(duration_nano)` group `service.name` | >1.5 s for 1 min |
| payment-timeouts | traces: count filter `service.name='payment' AND has_error=true` | >5/min |
| secret-leak | logs: `body REGEXP 'AKIA[0-9A-Z]{16}' OR body CONTAINS 'BEGIN RSA PRIVATE KEY'` + `deployment.environment='production'` | ≥1 |
| agentk-budget | metric `agentk.cost.usd` rate | >$2/hour |

**Dashboards (JSON in `provisioning/dashboards/`):**
1. *AstroMart Overview* — top-10 slowest routes (orderBy+limit), error rate by service, p99 by `service.version` (rollout comparison), failed-order value `sumIf(order.total, has_error=true)`, impacted users `count_distinct(user.id)`, per-tenant request rate with `having count() > 100`.
2. *Watch the Watcher* — investigations/hour, time-to-RCA histogram, tokens & $ per investigation (by model), tool error rate, LLM latency p95.
3. *LLM Cost* — cost by model / by investigation / cumulative, budget-alert threshold line.

## Appendix F — Repo conventions, Makefile, tests
- **Makefile:** `up` (signoz compose + app compose), `provision`, `incident-bad-deploy|pool|flags|leak`, `resolve`, `demo` (up→provision→wait→incident-bad-deploy), `logs`, `check` (ruff+pytest), `down`, `nuke` (down -v).
- **README structure:** hero GIF → what/why (3 sentences) → architecture diagram → 3-command quickstart (`cp .env.example .env` + fill 2 keys → `make up` → `make provision && make demo`) → scenario table → judging-criteria mapping table → security notes (docker socket, HMAC approval) → blog/video links.
- **Tests (light, meaningful):** unit — cost calc, HMAC approval sig, report rendering from a fixture investigation, webhook payload parsing from the captured fixture; integration (manual, documented) — `make demo` end-to-end. No test theater.
- **Code style:** small modules as named above, type hints everywhere, no premature abstraction (no BaseToolProvider hierarchies — two files `tools_mcp.py`/`tools_rest.py` with the same function signatures).

## Appendix G — Failure-mode playbook for the coding agent
- **MCP client friction >½ day (D3):** switch to `tools_rest.py` (SigNoz `query_range` v5 REST). Keep MCP server running and used for at least dashboard provisioning so "SigNoz MCP" stays truthfully in the story.
- **SigNoz webhook payload differs from Alertmanager shape:** the pydantic model lives in one file (`agent/models.py`); regenerate from the captured fixture.
- **Laptop RAM pressure:** drop loadgen RPS to 2, cap ClickHouse memory, close the OTel demo idea permanently (already rejected).
- **Array-attr (`feature_flags`) queries misbehave:** fall back to a comma-joined string attr + `CONTAINS` filter; note it in the blog as a finding (judges value real findings).
- **LLM provider rate limits / flaky endpoint during demo recording:** pre-warm one investigation, record from replay if needed; keep `OPENAI_BASE_URL` swappable so a backup provider is one env change away.
- **Provider's function-calling quality varies:** if the chosen model mangles tool calls, add strict JSON-schema `parameters`, lower temperature to 0, and reduce the tool count further (merge search+aggregate variants).

## Appendix H — Ordered checkpoints (each must pass before the next)
1. `make up` → SigNoz UI at :8080 shows traces from all 4 sandbox services + loadgen traffic. *(D1)*
2. `make incident-bad-deploy` → symptom visible in Explorer; deployment marker log present; `make resolve` recovers. *(D2)*
3. `make provision` → 5 alert rules + 3 dashboards exist; test-fire checkout-error-rate → webhook JSON logged by agent stub. *(D2)*
4. Agent answers `POST /investigate` with a correct RCA for bad-deploy using ≥6 distinct SigNoz tools; report saved + Slack message posted. *(D3)*
5. Full loop: alert → webhook → investigation → RCA in Slack → approve link → rollback → verification message. *(D4)*
6. All 4 scenarios produce correct root causes (run each twice). *(D4)*
7. Agent's own trace visible in SigNoz with gen_ai + cost attrs; Watch-the-Watcher + LLM Cost dashboards populated; budget alert fires under forced token burn. *(D5)*
8. Fresh-clone quickstart on a clean checkout reproduces checkpoint 5. *(D6, before recording video)*

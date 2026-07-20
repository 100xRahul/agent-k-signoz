# Agent K — Implementation Reference

> **Purpose:** Quick technical reference for the entire codebase. Read this instead of re-reading all source files. Kept up-to-date as changes are made.
>
> **Last updated:** 2026-07-19 (SigNoz self-host migrated to **Foundry** — SigNoz removed their compose manifests from the repo; we forge manifests from `deploy/signoz/casting.yaml` and commit `pours/`)

---

## Project Overview

**Agent K** is an autonomous AI SRE sidekick for the "Agents of SigNoz" hackathon. A SigNoz alert fires → webhook wakes Agent K → it investigates via SigNoz MCP/REST tools → posts RCA to Slack → proposes guarded remediation → and every step is itself OTel-instrumented into the same SigNoz.

**Stack:** Python 3.12, FastAPI, OpenAI-compatible LLM, SigNoz (self-hosted), MCP, SQLite, Docker Compose.

---

## Repo Layout

```
signoz-hacakethon/
├── docker-compose.yml           # App services (joins signoz_default network)
├── Makefile                     # up/down/provision/incident-*/demo/check
├── .env.example                 # Template for secrets
├── PLAN.md                      # Full spec (Appendices A-H)
├── IMPLEMENTATION.md            # ← THIS FILE
├── README.md                    # User-facing docs
│
├── agent/                       # Agent K core
│   ├── Dockerfile               # Python 3.12 + docker CLI (restart action) + uv.
│   │                            #   Build context = repo root (also copies provisioning/)
│   ├── pyproject.toml           # Dependencies
│   ├── config.py                # Settings (pydantic-settings, env vars)
│   ├── main.py                  # FastAPI app: webhooks, approvals, reports
│   ├── models.py                # Pydantic models: AlertmanagerWebhook, triggers
│   ├── loop.py                  # Agent reasoning loop (tool-use over OpenAI API)
│   ├── playbook.py              # System prompt (SRE runbook, 8 investigation steps)
│   ├── tools_mcp.py             # MCP client (streamable HTTP, 14-tool allowlist).
│   │                            #   MCP is the ONLY tool path — no REST fallback;
│   │                            #   MCP down = investigation fails loudly
│   ├── remediation.py           # Guarded actions: rollback, disable_flag, restart
│   ├── report.py                # signoz:// link rewriting, HTML rendering
│   ├── slack.py                 # Block Kit messages, HMAC approval links
│   ├── store.py                 # SQLite (WAL): investigations + actions tables
│   ├── telemetry.py             # OTel setup: traces, metrics, logs, gen_ai semconv
│   ├── templates/
│   │   ├── reports.html         # Reports list page (dark monospace)
│   │   └── report_detail.html   # Single report view (markdown rendered)
│   └── tests/
│       ├── test_agent.py        # 8 unit tests: cost calc, HMAC, link rewriting, webhook,
│       │                        #   mrkdwn conversion, remediation guards, cooldown lookup
│       └── fixtures/
│           └── webhook_sample.json  # Sample SigNoz alert payload
│
├── sandbox/                     # "AstroMart" mini-shop (4 FastAPI services)
│   ├── gateway/                 # Port 8001 — routes to checkout/inventory
│   │   ├── app.py               # POST /api/checkout, GET /api/products
│   │   ├── telemetry.py         # OTel setup (shared pattern across services)
│   │   ├── Dockerfile
│   │   └── pyproject.toml
│   ├── checkout/                # Port 8002 — order processing + chaos modes
│   │   ├── app.py               # POST /checkout (bad-deploy + flag-combo chaos)
│   │   ├── telemetry.py
│   │   ├── Dockerfile
│   │   └── pyproject.toml
│   ├── payment/                 # Port 8003 — Postgres INSERT + chaos modes
│   │   ├── app.py               # POST /pay (pool-exhaustion + secret-leak chaos)
│   │   ├── telemetry.py
│   │   ├── Dockerfile
│   │   └── pyproject.toml
│   ├── inventory/               # Port 8004 — Redis product catalog
│   │   ├── app.py               # GET /products, POST /reserve
│   │   ├── telemetry.py
│   │   ├── Dockerfile
│   │   └── pyproject.toml
│   ├── loadgen/                 # Background traffic generator
│   │   ├── main.py              # Async httpx loop, 70/30 checkout/products, 5 RPS
│   │   ├── Dockerfile
│   │   └── pyproject.toml
│   └── chaos/                   # Chaos scenario CLI
│       ├── __init__.py
│       ├── __main__.py          # Entry: python -m chaos <scenario>
│       ├── chaos.py             # 4 scenarios + resolve, Redis flags, deploy markers
│       ├── Dockerfile           # Slim python image — chaos is pure Redis + OTLP markers
│       └── pyproject.toml
│
├── provisioning/                # Dashboards + alerts as code
│   ├── __init__.py
│   ├── provision.py             # Idempotent SigNoz REST API provisioner
│   ├── dashboards/
│   │   ├── astromart_overview.json   # Shop metrics (QB v5 rubric queries)
│   │   ├── watch_the_watcher.json    # Agent meta-observability
│   │   └── llm_cost.json            # LLM token/cost tracking
│   └── alerts/
│       ├── checkout_error_rate.json  # >10% errors for 1 min
│       ├── checkout_p99_latency.json # >1.5s p99 for 1 min
│       ├── payment_timeouts.json     # >5 timeout errors/min
│       ├── secret_leak.json          # AKIA regex in prod logs
│       └── agentk_budget.json        # Agent cost >$2/hour
│
├── reports/                     # Generated RCA outputs (.gitkeep)
└── deploy/
    └── signoz/                  # SigNoz self-host via Foundry
        ├── casting.yaml         # Foundry install spec (+ alias patches)
        └── pours/deployment/    # Forged manifests (committed; `make forge-signoz` regenerates)
```

---

## Key Interfaces & Data Flow

### Alert → Investigation Flow
```
SigNoz Alert → POST /webhook/signoz (main.py)
  → parse AlertmanagerWebhook (models.py)
  → should_investigate() gate: skip resolved alerts, skip if already running,
    skip if same alertname investigated < INVESTIGATION_COOLDOWN_MINUTES ago
  → create investigation record (store.py)
  → run_investigation(trigger, investigation_id) (loop.py)
    → LLM loop with PLAYBOOK system prompt (playbook.py)
    → Tool calls via MCP (tools_mcp.py) or REST (tools_rest.py)
    → propose_remediation → store action + Slack msg (slack.py)
    → finish_investigation → rewrite links (report.py)
    → persist to SQLite (store.py)
    → post RCA to Slack (slack.py)
```

### Manual Trigger
```
POST /investigate {prompt: "..."} → InvestigationTrigger.from_manual() → same loop
```

### Approval Flow
```
Slack msg with HMAC-signed link → GET /approve/{action_id}?sig=...
  → verify HMAC → execute_action (remediation.py) → verify → Slack follow-up
```

---

## Configuration (agent/config.py)

All via env vars (pydantic-settings `Settings` class, singleton at `config.settings`):

| Env Var | Default | Purpose |
|---------|---------|---------|
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | LLM endpoint |
| `OPENAI_API_KEY` | `sk-placeholder` | LLM auth |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model name |
| `LLM_INPUT_PRICE_PER_MTOK` | `0` | Cost per M input tokens |
| `LLM_OUTPUT_PRICE_PER_MTOK` | `0` | Cost per M output tokens |
| `SIGNOZ_URL` | `http://localhost:8080` | Browser-facing SigNoz URL |
| `SIGNOZ_API_KEY` | `""` | SigNoz API auth key |
| `MCP_URL` | `http://mcp-server:8000` | MCP server internal URL |
| `SLACK_WEBHOOK_URL` | `""` | Slack incoming webhook |
| `AGENT_PUBLIC_URL` | `http://localhost:9000` | Agent's external URL |
| `APPROVAL_SECRET` | `change-me...` | HMAC key for approval links |
| `AUTO_APPROVE` | `false` | Skip approval for demo |
| `MAX_ITERATIONS` | `20` | Max LLM tool-use loops |
| `MAX_COST_USD_PER_INVESTIGATION` | `1.00` | Budget cap per investigation |
| `INVESTIGATION_COOLDOWN_MINUTES` | `15` | Don't re-investigate same alertname within window |
| `DB_PATH` | `/data/agentk.db` | SQLite path |
| `REDIS_URL` | `redis://sandbox-redis:6379` | Redis for chaos flags |

---

## Database Schema (agent/store.py)

SQLite in WAL mode. Singleton `store` instance. Two tables:

```sql
investigations(id TEXT PK, trigger_json TEXT, status TEXT,  -- running|done|failed
  started_at TEXT, finished_at TEXT, report_md TEXT, root_cause TEXT,
  cost_usd REAL, tokens_in INT, tokens_out INT, trace_id TEXT);

actions(id TEXT PK, investigation_id TEXT FK, kind TEXT, params_json TEXT,
  status TEXT,  -- proposed|approved|executed|verified|rejected
  created_at TEXT, executed_at TEXT, verification_md TEXT);
```

---

## Agent Loop (agent/loop.py)

Hand-rolled tool-use loop over OpenAI chat-completions (**AsyncOpenAI** — a sync
client would block the event loop and freeze webhooks/approvals during LLM calls):

1. Messages start with `[system: PLAYBOOK, user: render_trigger(trigger)]`
2. For each iteration (up to `MAX_ITERATIONS`):
   - LLM call → wrapped in `llm_call_span` (gen_ai semconv)
   - If `finish_investigation` tool: persist report, notify Slack, break
   - If `propose_remediation` tool: create action record, Slack msg, optionally auto-execute
   - Else: route to MCP/REST tool → wrapped in `tool_call_span`
   - Truncate tool results to 15k chars
   - Budget check: if cost > max, finish with partial report (status stays `done`)
3. **Guaranteed report:** at `MAX_ITERATIONS - 2` a wrap-up nudge is appended and
   `tool_choice` is forced to `finish_investigation` — no investigation ends empty
4. Persist final results to SQLite, set span attributes, post to Slack

### Tool Allowlist (16 tools)
**SigNoz MCP tools (14):** `signoz_list_services`, `signoz_get_service_top_operations`, `signoz_aggregate_traces`, `signoz_search_traces`, `signoz_get_trace_details`, `signoz_search_logs`, `signoz_aggregate_logs`, `signoz_query_metrics`, `signoz_execute_builder_query`, `signoz_get_field_keys`, `signoz_get_field_values`, `signoz_list_alerts`, `signoz_get_alert`, `signoz_get_alert_history`

The field-discovery tools (`get_field_keys`/`get_field_values`) let the agent adapt to systems whose attribute schema it has never seen — the playbook tells it to discover attributes before filtering on them.

**Synthetic tools (2):** `finish_investigation(report_markdown, root_cause_oneliner, confidence)`, `propose_remediation(kind, service, reason)`

### Tool Routing
MCP only (`tools_mcp.py` — streamable HTTP to `mcp-server:8000/mcp`). No REST fallback: if MCP is unreachable the investigation is marked `failed` with a clear report (fail loudly, never degrade silently). Remediation verification queries SigNoz directly via `query_range` v5 (`remediation.py:_error_count`).

---

## Chaos Scenarios (sandbox/chaos/chaos.py)

Flags stored in Redis as `chaos:<name> = "1"`. Services check per-request.
**No docker-in-docker anywhere:** a "deploy" is modeled by the
`chaos:checkout-version` Redis key — checkout reads it per request and stamps
`service.version` on every span/log, so version changes need no container
restart. Deploy/rollback markers are emitted from `service.name=deploy-bot`.

| Scenario | Redis Keys | Mechanism | Alert Trigger |
|----------|-----------|-----------|---------------|
| `bad-deploy` | `chaos:bad-deploy`, `chaos:checkout-version=1.4.2` | Checkout: +800ms latency, 35% errors, spans tagged v1.4.2. Deploy marker emitted. | checkout-error-rate, checkout-p99 |
| `pool-exhaustion` | `chaos:pool-exhaustion` | Payment: holds DB connections 5s → pool starves | payment-timeouts |
| `flag-combo` | `chaos:flag-combo` | Checkout: 25% error ONLY when `new-checkout` AND `express-pay` both present | checkout-error-rate |
| `secret-leak` | `chaos:secret-leak` | Payment: logs `AKIAIOSFODNN7EXAMPLE` pattern in error logs | secret-leak |
| `resolve` | clears all, version→1.4.1 | Clears flags, restores baseline version, emits rollback marker | — |

---

## Remediation (agent/remediation.py)

Guarded, allow-listed actions. Target comes from the LLM as `service`
(`flag` accepted as alias — `_normalize_target`). Failures return `❌ ...`
strings instead of raising.

| Action | Run | Verify |
|--------|-----|--------|
| `rollback` | Redis: `chaos:checkout-version=1.4.1`, delete `chaos:bad-deploy`; emit rollback marker | Wait 60s, **re-query SigNoz** for the service's error count (shape-tolerant parse); report the actual number |
| `disable_flag` | Delete `chaos:<flag>` — only flags in `KNOWN_FLAGS` | Wait 30s, confirm key gone |
| `restart` | `docker restart` on container found by `com.docker.compose.service` label (project-name agnostic); only services in `ALLOWED_SERVICES`; subprocess via `asyncio.to_thread` | Wait 15s, confirm container running |

Docker socket (ro) is mounted **only** in the agent, **only** for `restart`.

---

## Telemetry (agent/telemetry.py)

### Spans
- **Root:** `investigation` — attrs: `agentk.trigger_type`, `agentk.alertname`, `agentk.root_cause`, `agentk.confidence`, `agentk.total_cost_usd`
- **Child:** `llm.call` — attrs: `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `llm.cost.usd`
- **Child:** `tool.<name>` — attrs: `tool.name`, `tool.args_summary`, `tool.result_bytes`, `error`

### Metrics
- `agentk.investigations` (counter, by alertname)
- `agentk.investigation.duration` (histogram, by alertname)
- `agentk.cost.usd` (counter, by model)

---

## Sandbox Service Telemetry Pattern

All 4 services share the same `telemetry.py` pattern:
```python
Resource.create({"service.name": name})  # Merges with OTEL_RESOURCE_ATTRIBUTES env
TracerProvider + BatchSpanProcessor → OTLPSpanExporter (gRPC :4317)
MeterProvider + PeriodicExportingMetricReader → OTLPMetricExporter
LoggerProvider + BatchLogRecordProcessor → OTLPLogExporter
FastAPIInstrumentor.instrument_app(app)
```

### Key Span Attributes (designed for QB v5 rubric)
- `service.version` — rollout comparison
- `deployment.environment` — always "production"
- `order.total` (float) — for `sumIf` blast radius
- `user.id` — for `count_distinct` impact
- `tenant.name` — for per-tenant group-by
- `feature_flags` (array) — for `hasAll` filter
- `has_error` (bool) — for error rate queries

---

## Provisioning (provisioning/provision.py)

Idempotent REST-based provisioner against SigNoz API v1:
1. Creates notification channels: `agent-k-webhook` (→ agent:9000) + `agent-k-slack`
2. Creates 5 alert rules from `alerts/*.json` (injects channel IDs)
3. Creates 3 dashboards from `dashboards/*.json`

Runs via `make provision` → `docker compose exec agent python -m provisioning.provision`

---

## Docker Compose Topology

Two compose layers, one `signoz-network` network. SigNoz layer is **forged by
Foundry** (`deploy/signoz/casting.yaml` → `pours/deployment/compose.yaml`,
committed). Casting patches add network aliases `signoz` (UI/API container
`signoz-signoz-0`) and `signoz-otel-collector` (ingester) so app code keeps the
canonical hostnames:

| Service | Build/Image | Port | Key Role |
|---------|-------------|------|----------|
| signoz + ingester (Foundry) | signoz/signoz, signoz/signoz-otel-collector, clickhouse, postgres | 8080, 4317/4318 | Observability platform |
| mcp-server | `signoz/signoz-mcp-server:latest` | 8000 | SigNoz MCP server |
| sandbox-postgres | `postgres:16-alpine` | — | Payment DB |
| sandbox-redis | `redis:7-alpine` | — | Chaos flags + inventory |
| gateway | `./sandbox/gateway` | 8001 | API gateway |
| checkout | `./sandbox/checkout` | 8002 | Order processing |
| payment | `./sandbox/payment` | 8003 | Payment + DB |
| inventory | `./sandbox/inventory` | 8004 | Product catalog |
| loadgen | `./sandbox/loadgen` | — | Traffic generator |
| chaos | `./sandbox/chaos` | — | Chaos CLI (profiles: tools) |
| agent | `./agent` (context: root) | 9000 | Agent K |

---

## Makefile Targets

| Target | What it does |
|--------|-------------|
| `make up` | Start SigNoz (forged compose) + app compose; waits on `/api/v1/health` |
| `make forge-signoz` | Regenerate SigNoz manifests via foundryctl (only to bump SigNoz) |
| `make down` | Stop everything |
| `make nuke` | Stop + remove all volumes |
| `make provision` | Provision dashboards + alerts into SigNoz |
| `make incident-bad-deploy` | Trigger bad-deploy chaos |
| `make incident-pool` | Trigger pool-exhaustion chaos |
| `make incident-flags` | Trigger flag-combo chaos |
| `make incident-leak` | Trigger secret-leak chaos |
| `make resolve` | Clear all chaos scenarios |
| `make demo` | up → provision → wait → incident-bad-deploy |
| `make logs` / `make logs-<svc>` | Tail logs |
| `make check` | ruff + pytest |

---

## Known Design Decisions

1. **Hand-rolled agent loop** (not LangChain) — smaller, fully traceable, reads better in blog
2. **MCP + REST fallback** — try MCP first, fall back to direct REST if unavailable
3. **Redis-driven deploys, no docker-in-docker** — checkout reads `chaos:checkout-version` per request; "deploy"/"rollback" = key flip + deploy-bot marker log. Only the `restart` action touches docker (ro socket, label-based lookup)
4. **HMAC approval links** — no Slack app needed, just incoming webhook + signed URLs
5. **signoz:// placeholders** — LLM outputs `signoz://trace/<id>`, report.py rewrites to real URLs
6. **SQLite** — zero-ops, good enough for hackathon scale; also backs the alert cooldown gate
7. **Chaos via Redis flags** — services check per-request, cheap GET cached 1s
8. **Alert gating** — resolved alerts never investigated; per-alertname cooldown (`INVESTIGATION_COOLDOWN_MINUTES`) stops re-fire token burn
9. **Agent image from root context** — carries `provisioning/` so `make provision` runs inside the agent container; `.dockerignore` keeps context slim
10. **Compose env defaults** — `OPENAI_BASE_URL`/`OPENAI_API_KEY` fall back to sane defaults in docker-compose.yml so an unset var never injects an empty string over the code default

---

## Verification Checkpoints (from PLAN.md Appendix H)

1. `make up` → SigNoz UI shows traces from all 4 services
2. `make incident-bad-deploy` → symptoms visible in Explorer
3. `make provision` → 5 alerts + 3 dashboards exist; webhook fires
4. Agent produces correct RCA using ≥6 SigNoz tools
5. Full loop: alert → webhook → investigation → Slack RCA → approve → rollback → verify
6. All 4 scenarios produce correct root causes
7. Agent's own trace visible with gen_ai + cost attrs; dashboards populated
8. Fresh-clone quickstart reproduces checkpoint 5

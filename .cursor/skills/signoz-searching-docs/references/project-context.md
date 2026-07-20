# Agent K / signoz-hacakethon — project context

Use this file when questions involve **this repo's** self-hosted SigNoz, MCP wiring, or API behavior. Always prefer official docs via `signoz_search_docs` / `signoz_fetch_doc`; this file captures gaps where docs lag or this project diverges.

## Installed Cursor skills (`.cursor/skills/`)

| Skill | Use when |
|---|---|
| `signoz-mcp-setup` | `signoz_*` tools missing; MCP auth/URL issues |
| `signoz-searching-docs` | How-to, official docs, instrumentation guides |
| `signoz-generating-queries` | Ad-hoc metrics/logs/traces queries (Agent K playbook steps) |
| `signoz-investigating-alerts` | RCA for a firing/recent alert (mirrors Agent K loop) |
| `signoz-explaining-alerts` | Decode alert rule JSON in `provisioning/alerts/` |
| `signoz-creating-alerts` | Author new alert rules as code |
| `signoz-creating-dashboards` | Author new dashboards as code |
| `signoz-modifying-dashboards` | Edit existing dashboard JSON |
| `signoz-explaining-dashboards` | Explain `provisioning/dashboards/` panels |
| `signoz-setting-up-observability` | OTel instrumentation in `sandbox/` or `agent/` |

**Not installed** (low value for this repo): `signoz-managing-views`, `signoz-writing-clickhouse-queries`, `signoz-reducing-telemetry-cost`.

**MCP endpoint for Cursor:** `.cursor/mcp.json` → `http://localhost:8000/mcp` (requires `make up` + bootstrap).

## What this project is

- **Agent K** — autonomous on-call SRE agent for the SigNoz "Agents of SigNoz" hackathon (WeMakeDevs × SigNoz, July 2026).
- AstroMart sandbox (gateway → checkout → payment → inventory) + Agent K loop investigating via SigNoz MCP/API v5.
- SigNoz UI: `http://localhost:8080` · Agent reports: `http://localhost:9000/reports` · MCP: `http://localhost:8000/mcp`.

## Local stack

```bash
make up          # SigNoz (Foundry) + sandbox + mcp-server
make bootstrap   # admin account, service account, API key → .env
docker compose up -d mcp-server agent
make provision   # dashboards + alerts as code
```

- **MCP server** (`docker-compose.yml` → `mcp-server`): `signoz/signoz-mcp-server:latest`, `TRANSPORT_MODE=http`, `SIGNOZ_URL=http://signoz:8080`, port `8000:8000`.
- **Agent** uses `MCP_URL=http://mcp-server:8000` internally; Cursor uses `http://localhost:8000/mcp` via `.cursor/mcp.json`.
- Secrets live in `.env` (git-ignored): `OPENAI_API_KEY`, `SLACK_WEBHOOK_URL`, `SIGNOZ_API_KEY`.

## SigNoz self-host (Foundry)

As of July 2026, SigNoz removed `deploy/docker/` compose manifests from their repo. This project commits Foundry output so clones work without `foundryctl`:

- `deploy/signoz/casting.yaml` — install spec; network aliases `signoz` and `signoz-otel-collector` keep canonical hostnames.
- Forged manifests: `deploy/signoz/pours/deployment/compose.yaml` on network `signoz-network`.
- `make forge-signoz` regenerates pours; `make up` uses plain docker compose.
- Health: `GET http://localhost:8080/api/v1/health`.

## SigNoz v0.133 API (probed; docs may lag)

- **Login:** `POST /api/v2/sessions/email_password` `{email, password, orgID}` → `data.accessToken`. `/api/v1/login` is gone.
- **First boot:** `POST /api/v1/register` `{email, password, firstName, orgName}`; OTLP ingestion works after setup completes.
- **API keys:** `POST /api/v1/service_accounts` → `POST /api/v1/service_accounts/{id}/keys` → `data.key`; auth header `SIGNOZ-API-KEY`.
- **Alert rules:** use `"version": "v5"` + `condition.compositeQuery.queries` with `type: "builder_query"`; legacy `builderQueries` rejected.
- **Channels:** webhook receiver key is `url` (not `api_url`).
- **Dashboards:** `POST /api/v1/dashboards` expects content directly (title, widgets); wrapping in `{"data": ...}` double-nests silently.
- **Query:** `POST /api/v5/query_range` with `start`/`end` in ms, `requestType`, `compositeQuery.queries`.
- **Explorer deep links:** `/traces-explorer?compositeQuery=...&startTime=<ns>&endTime=<ns>`; trace view `/trace/{id}` (`agent/report.py` → `link_explorer`).

## OTel / instrumentation in this repo

- Sandbox services and Agent K export OTLP to the SigNoz collector.
- Agent spans include `gen_ai.*` semconv; checkout stamps `service.version` per-request (static `OTEL_RESOURCE_ATTRIBUTES` would shadow span attrs in group-bys).
- Provisioning: `provisioning/dashboards/`, `provisioning/alerts/` (JSON as code).

## Port conflicts

If `4317`/`4318`/`8080` are taken, check for other stacks (e.g. `thalus-apps-observability`) binding those ports before debugging SigNoz startup.

## Key paths

| Path | Purpose |
|---|---|
| `agent/` | Agent K loop, MCP tools, remediation, Slack RCA |
| `sandbox/` | AstroMart microservices + chaos CLI |
| `provisioning/` | Dashboards, alerts, bootstrap |
| `deploy/signoz/` | Foundry casting + forged compose |
| `IMPLEMENTATION.md` | Build notes |

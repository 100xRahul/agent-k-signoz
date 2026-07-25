# Agent K — Live Test Log

## Phase 0 boot — 2026-07-20T16:30:00Z

- **Stack:** `make up` + `make bootstrap` — SigNoz `:8080` and Agent `:9000` healthy
- **Credentials:** `OPENAI_API_KEY` and `SIGNOZ_API_KEY` configured; `SLACK_WEBHOOK_URL` empty (console RCA fallback active)
- **Telemetry:** Services `gateway`, `checkout`, `payment`, `inventory`, `agent-k` visible in SigNoz within ~2 min
- **Manual investigate:** `POST /investigate` completed (`done`), report mentions checkout, cost ~$0.04, ~178s
- **MCP finding:** `hasAll(feature_flags, [...])` fails on trace span attributes — use dual `CONTAINS` instead

## Provisioning audit — 2026-07-20T16:45:00Z

| Resource | Expected | Result |
|----------|----------|--------|
| Dashboards | 4 | ✅ AstroMart, Watch the Watcher, LLM Cost, QB v5 Rubric |
| Alerts | 7 | ✅ All created via `make provision-fresh` |
| Views | 6 | ✅ Fixed — `compositeQuery.panelType: "list"` required |
| Channels | agent-k-webhook | ✅ Exists |

**Fix applied:** Explorer views returned 400 until `panelType: "list"` added to `compositeQuery`. `make provision-fresh` deletes and recreates Agent K resources.

## Slack-free UX

When `SLACK_WEBHOOK_URL` is empty, agent logs bordered RCA summary and `Approve: http://localhost:9000/approve/{id}?sig=...` to stdout.

## Bad-deploy E2E — 2026-07-20T16:47:00Z

- **Scenario:** `make incident-bad-deploy` + `scripts/fire_webhook.py bad-deploy`
- **Investigation:** `4a26a020975b` — status `done`, cost ~$0.06
- **Root cause excerpt:** "checkout v1.4.2 (deployed 16:33:51) introduced a fault in checkout.process_order..."
- **Remediation:** `rollback(checkout)` proposed; `AUTO_APPROVE=true` executed rollback to v1.4.1 immediately
- **Console output:** RCA summary + approval URL logged (Slack skipped)

## Flag-combo spot check — 2026-07-20T16:40:00Z

- **Investigation:** `706a38a9229e` — status `done`
- **Root cause excerpt:** "flag-combo: new-checkout + express-pay conflict whenever an order carries both flags"
- **Remediation:** `disable_flag(flag-combo)` proposed; approval URL printed to console

## Dashboard smoke — 2026-07-20T16:48:00Z

- **QB v5 Rubric Showcase** and **Watch the Watcher** dashboards present in SigNoz (4 Agent K dashboards total)
- **Watch the Watcher:** agent-k investigations show `gen_ai.usage.*` on spans; cost > $0 during live runs
- **QB v5 Rubric:** panels provisioned; live data visible during bad-deploy incident window

## Live test run — 2026-07-20T16:50:00Z

Result: **PASS** (manual E2E verification)

- PASS health SigNoz
- PASS health Agent
- PASS manual investigation (checkout mentioned)
- PASS bad-deploy RCA (v1.4.2 + rollback)
- PASS flag-combo RCA (new-checkout + express-pay)
- PASS provision-fresh (7 alerts, 4 dashboards, 6 views)
- PASS JSON API `GET /api/investigations`

# 🕶️ Agent K — Your AI SRE Sidekick

> *"Agents of SigNoz" Hackathon — WeMakeDevs × SigNoz, July 2026*

An autonomous on-call SRE agent that investigates production incidents through SigNoz, delivers root-cause analysis with business blast radius to Slack, and executes guarded remediation — all while being fully observable in the same SigNoz instance it uses to debug your systems.

**SigNoz observes the agent that observes SigNoz.**

## Architecture

```
┌─────────────── incident sandbox ────────────────┐
│ AstroMart: gateway → checkout → payment →       │
│ inventory (FastAPI ×4) + Postgres + Redis       │
│ + loadgen (steady traffic) + chaos CLI          │
└───────────────┬─────────────────────────────────┘
                │ OTLP
        ┌───────▼────────┐   alert webhook   ┌──────────────┐
        │     SigNoz     │──────────────────▶│   AGENT K    │
        │ (self-hosted)  │◀──────────────────│ FastAPI +    │
        └───────▲────────┘  MCP / API v5     │ LLM agent   │
                │ OTLP (agent's own traces,  │ loop         │
                │ gen_ai.* spans, cost)      └──────┬───────┘
                └───────────────────────────────────┘
                                             Slack (RCA + approval)
```

## 🚀 Quickstart (3 commands)

```bash
# 1. Configure
cp .env.example .env
# Edit .env: add OPENAI_API_KEY + SLACK_WEBHOOK_URL

# 2. Start everything
make up
# → SigNoz UI at http://localhost:8080
# → Create API key in SigNoz Settings → API Keys, add to .env as SIGNOZ_API_KEY
# → Restart: make down && make up

# 3. Provision & demo
make provision && make demo
```

## 📊 Incident Scenarios

| Scenario | Command | What happens | How Agent K finds it |
|---|---|---|---|
| **Bad Deploy** | `make incident-bad-deploy` | Checkout v1.4.2 adds 800ms latency + 35% errors | Version correlation, deploy marker log |
| **Pool Exhaustion** | `make incident-pool` | Payment DB pool starves → timeouts cascade | Payment latency spike, timeout errors |
| **Flag Combo** | `make incident-flags` | Errors only when `hasAll(feature_flags, [new-checkout, express-pay])` | Array attribute filter isolation |
| **Secret Leak** | `make incident-leak` | AWS key pattern leaks into prod logs | Regex log search |
| **Resolve** | `make resolve` | Clears all chaos, restores healthy state | Recovery visible in graphs |

## 🏆 Judging Criteria Mapping

| Criterion | How Agent K delivers |
|---|---|
| **Impact** | Real on-call pain solved; MTTR from alert→RCA in minutes; business-value blast radius |
| **Creativity** | Closed loop: agent debugs via SigNoz *and* is observed by SigNoz; self-investigating budget alert |
| **Technical Excellence** | Full OTel semconv (incl. gen_ai), dashboards/alerts-as-code, deterministic chaos harness, guarded remediation |
| **Best Use of SigNoz** | All signals + dashboards + alerts + MCP server + QB v5 rubric queries as the playbook |
| **UX** | Slack-native RCA with deep links + one-click approve; `/reports` web page; 3-command quickstart |
| **Presentation** | Scripted demo video, architecture diagram, quality blog |

## 🔒 Security Notes

- **Docker socket**: Agent K mounts the Docker socket (read-only) for remediation actions (rollback/restart). This is documented and intentional for the demo.
- **HMAC approval**: Remediation actions require HMAC-signed approval links, preventing unauthorized execution.
- **API keys**: All secrets are in `.env` (git-ignored). Never commit real keys.

## 🛠️ Development

```bash
make logs          # Follow all logs
make logs-agent    # Follow agent logs only
make check         # Run ruff + pytest
make down          # Stop everything
make nuke          # Stop + remove volumes
```

## 📁 Project Structure

```
agent-k/
├─ docker-compose.yml          # SigNoz + sandbox + mcp-server + agent + loadgen
├─ Makefile                    # up / provision / incident-* / demo / down
├─ sandbox/
│  ├─ gateway/                 # API gateway (FastAPI)
│  ├─ checkout/                # Order processing + chaos modes
│  ├─ payment/                 # Payment service + Postgres
│  ├─ inventory/               # Product inventory + Redis
│  ├─ loadgen/                 # Traffic generator
│  └─ chaos/                   # Chaos scenario CLI
├─ agent/
│  ├─ main.py                  # FastAPI endpoints
│  ├─ loop.py                  # Agent reasoning loop
│  ├─ playbook.py              # SRE investigation runbook
│  ├─ tools_mcp.py             # SigNoz MCP client
│  ├─ tools_rest.py            # REST fallback
│  ├─ remediation.py           # Guarded actions
│  ├─ telemetry.py             # gen_ai OTel spans + cost
│  ├─ report.py                # RCA report generation
│  ├─ slack.py                 # Slack Block Kit integration
│  └─ store.py                 # SQLite state
├─ provisioning/
│  ├─ dashboards/              # Dashboard JSON definitions
│  ├─ alerts/                  # Alert rule definitions
│  └─ provision.py             # Idempotent provisioning script
├─ reports/                    # Generated RCA reports
└─ docs/                       # Architecture diagrams, demo script
```

---

*Built with ❤️ for the "Agents of SigNoz" hackathon by a human + Claude Code team.*

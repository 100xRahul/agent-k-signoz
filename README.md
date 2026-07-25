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

## 🚀 Quickstart

```bash
# 1. Configure
cp .env.example .env
# Edit .env: add OPENAI_API_KEY + SLACK_WEBHOOK_URL

# 2. Start everything (SigNoz self-host via committed Foundry manifests + the stack)
make up

# 3. First-boot SigNoz setup — admin account, service account, API key → .env
make bootstrap
docker compose up -d mcp-server agent   # pick up the new API key

# 4. Alerts + dashboards + views as code, then a scripted incident
make provision && make demo
```

SigNoz UI: http://localhost:8080 · Agent K reports: http://localhost:9000/reports · **QB v5 Rubric** dashboard · **Watch the Watcher** meta-observability

### Demo targets

| Command | Purpose |
|---------|---------|
| `make demo` | Full stack + bad-deploy incident |
| `make demo-full` | demo + wait for RCA + verify script |
| `make incident-budget` | Self-investigation budget spike demo |
| `make verify-rubric` | Run all 4 chaos scenarios |
| `make verify` | Health + manual investigation check |
| `make benchmark` | agentk-bench: scored run + 0%-false-alarm control → `docs/benchmark/` |
| `make verify-ledger` | Prove the hash-chained audit ledger is intact |

### Measured, checked, and provable

Beyond the demo, three things a judge can verify independently:

- **Measured, not demoed** — `make benchmark` scores live investigations
  deterministically (detection / localization / classification / remediation +
  **0% false alarms** on a healthy control) into `docs/benchmark/BENCHMARK.md`.
  Deterministic scoring means a run can't pass on a hallucinated RCA.
- **The writer is checked, not trusted** — an independent auditor model screens
  every finished RCA for groundedness before publish; ungrounded reports are
  badged and counted (`agentk.audit.groundedness`, `agentk-ungrounded-rca` alert).
- **Governed / provable** — every step is sealed into a hash-chained ledger;
  `make verify-ledger` recomputes the chain and proves it was not tampered with.

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
| **Creativity** | Closed loop: agent debugs via SigNoz *and* is observed by SigNoz; self-investigating budget alert; independent auditor screens its own RCAs |
| **Technical Excellence** | Full OTel semconv (incl. gen_ai), dashboards/alerts-as-code, deterministic chaos harness, guarded remediation, hash-chained audit ledger, **scored benchmark with 0% false alarms** |
| **Best Use of SigNoz** | All signals + dashboards + alerts + **MCP server** + **QB v5 rubric dashboard** + saved views |
| **UX** | Slack-native RCA with deep links + one-click approve; `/reports` web page; 6 saved Explorer views |
| **Presentation** | [Demo script](docs/demo-script.md) · [Submission blog](blog/submission-blog.md) · sample RCAs in `reports/` |

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
│  ├─ main.py                  # FastAPI endpoints (+ ledger verify/read)
│  ├─ loop.py                  # Agent reasoning loop (+ auditor gate, ledger seals)
│  ├─ auditor.py               # Independent groundedness auditor (2nd LLM pass)
│  ├─ playbook.py              # SRE investigation runbook
│  ├─ tools_mcp.py             # SigNoz MCP client (the only tool path — fail loud)
│  ├─ remediation.py           # Guarded actions
│  ├─ telemetry.py             # gen_ai OTel spans + cost + audit metric
│  ├─ report.py                # RCA report generation + audit badge
│  └─ store.py                 # SQLite state + hash-chained ledger
├─ scripts/
│  ├─ benchmark.py             # agentk-bench (scored, deterministic)
│  ├─ verify_ledger.py         # Prove the ledger chain is intact
│  └─ _harness.py              # Shared live-run plumbing
├─ provisioning/
│  ├─ dashboards/              # 4 dashboards incl. QB v5 Rubric Showcase
│  ├─ alerts/                  # 8 alert rules (incl. agentk-ungrounded-rca)
│  ├─ views/                   # 6 saved Explorer views
│  └─ provision.py             # Idempotent provisioning script
├─ reports/                    # Sample RCAs + generated reports
├─ scripts/                    # verify_checkpoints, incident_budget, verify_rubric
└─ docs/                       # demo-script.md, architecture.md
```

---

*Built with ❤️ for the "Agents of SigNoz" hackathon by a human + Claude Code team.*

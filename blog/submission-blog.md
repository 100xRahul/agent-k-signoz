# I Built an AI SRE That Debugs Through SigNoz — and SigNoz Watches It Back

> Submission blog for the Agents of SigNoz hackathon (WeMakeDevs × SigNoz, July 2026).
> Publish on Dev.to / Medium alongside the public repo and demo video.

---

## The problem

If you can't observe your AI agents, you don't own them. On-call engineers already live in observability tools — but when an alert fires at 3am, they still manually pivot between traces, logs, metrics, and deploy timelines. AI agents can automate that investigation loop, but only if they're grounded in real telemetry and accountable for their own cost.

## What I built: Agent K

**Agent K** is an autonomous SRE sidekick. A SigNoz alert fires → webhook wakes the agent → it investigates through the **official SigNoz MCP server** → posts a root-cause analysis with business blast radius to Slack → proposes guarded remediation (rollback / disable flag / restart) on human approval → and every step is OTel-instrumented into the **same** SigNoz instance.

**SigNoz observes the agent that observes SigNoz.**

## SigNoz features used (explicit checklist)

| Feature | How Agent K uses it |
|---------|---------------------|
| **Self-hosted SigNoz** | Foundry-forged compose stack, committed manifests |
| **Traces** | AstroMart sandbox (4 FastAPI services), agent investigation spans |
| **Logs** | Structured JSON logs, deploy-bot markers, secret-leak detection |
| **Metrics** | `agentk.investigations`, `agentk.cost.usd`, RED panels |
| **Query Builder v5** | Investigation playbook + dedicated rubric dashboard |
| **Alerts** | 7 rules as code: error-rate %, p99, flag-combo, secret-leak, budget, investigation-failed |
| **Dashboards** | AstroMart Overview, QB v5 Rubric, Watch the Watcher, LLM Cost |
| **Saved Explorer views** | 6 provisioned views (failing checkouts, deploy markers, MCP tools, …) |
| **MCP server** | 14 curated SigNoz tools via streamable HTTP |
| **gen_ai semconv** | `gen_ai.usage.*`, `llm.cost.usd` on every LLM call span |
| **Notification channels** | Webhook → Agent K + Slack |

## Query Builder v5 as the investigation playbook

Judges highlighted QB v5 capabilities (#11674–78). Agent K's playbook encodes them as a runbook:

- `hasAll(feature_flags, [...])` for flag-combo isolation
- `NOT EXISTS` for missing telemetry
- Regex log search for secret leaks
- `sumIf(order.total, has_error=true)` + `count_distinct(user.id)` for blast radius
- Rollout comparison via `attribute.service.version`
- Deploy Guardian: correlate deploy-bot marker logs with error onset

A dedicated **QB v5 Rubric Showcase** dashboard proves each capability with live panels.

## Meta-observability: Watch the Watcher

Every investigation is a trace on `agent-k` with child spans for each LLM call and MCP tool invocation. Dashboards track time-to-RCA, tokens per model, tool error rate, and cumulative LLM cost. When `agentk-budget-spike` fires, Agent K can **investigate itself** — the demo's closing one-liner.

## Deterministic demo sandbox

Four chaos scenarios (`make incident-*`) pair 1:1 with alert rules:

| Scenario | Root cause Agent K finds |
|----------|-------------------------|
| bad-deploy | checkout v1.4.2 regression |
| flag-combo | hasAll feature flag interaction |
| pool-exhaustion | payment DB pool starvation |
| secret-leak | AKIA + RSA key in prod logs |

`make demo-full` runs the full loop: up → bootstrap → provision → incident → RCA → approve → verify.

## Challenges

1. **Resource vs span attributes** — `service.version` for dynamic deploys must be span-scoped; resource attrs shadow group-bys.
2. **Alert eval delay** — SigNoz evaluates 2 minutes in the past; budget 3 min for demo alerts.
3. **MCP-only tool path** — fail loud if MCP is down; no silent REST fallback.

## Try it

```bash
git clone <repo>
cp .env.example .env   # add OPENAI_API_KEY + SLACK_WEBHOOK_URL
make up && make bootstrap && make provision && make demo
```

Repo: [GitHub link] · Demo video: [YouTube link] · Pre-event blog: [Query Builder v5 deep-dive link]

---

*Built for the Agents of SigNoz hackathon — one human + Claude Code.*

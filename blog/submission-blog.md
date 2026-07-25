# I Built an AI SRE That Debugs Through SigNoz — and SigNoz Watches It Back

> **Agents of SigNoz** hackathon submission (WeMakeDevs × SigNoz, July 2026) · Track 1: AI & Agent Observability
> Repo: https://github.com/100xRahul/agent-k-signoz · Demo: _<add your YouTube link>_

---

## The 3 AM problem

Every on-call engineer knows the drill. An alert fires. You open your observability tool, and then you start _pivoting_ — traces here, logs there, a metrics dashboard in another tab, the deploy timeline in a fourth. You hold the whole incident in your head while you correlate it by hand, half-awake, hoping you don't miss the one span that explains everything.

AI agents are supposed to automate exactly this kind of drudgery. But an agent turned loose on production is only trustworthy if two things are true: it's **grounded in real telemetry** (not vibes), and it's **as observable as the systems it debugs** (so you can see what it did, what it cost, and why it decided what it decided).

So I built **Agent K** — an autonomous SRE sidekick that investigates incidents _through_ SigNoz, and is _itself_ fully instrumented back _into_ the same SigNoz instance.

**SigNoz observes the agent that observes SigNoz.**

## What Agent K does, end to end

1. A SigNoz **alert** fires (e.g. checkout error-rate > 10%).
2. SigNoz's notification webhook wakes Agent K.
3. The agent runs an LLM tool-use loop, investigating **entirely through the official SigNoz MCP server** — listing services, aggregating traces, drilling into logs, running Query Builder v5 aggregations, correlating deploy markers.
4. It writes a **root-cause analysis** with a business blast radius (dollars of failed orders, affected users) and posts it to **Slack** with SigNoz deep links.
5. It proposes a **guarded remediation** (rollback / disable-flag / restart) behind an HMAC-signed approval link. A human clicks; the agent executes and then **re-queries SigNoz to verify** the fix worked.
6. Every step of that — every LLM call, every tool call, the cost, the verdict — is emitted as OpenTelemetry back into SigNoz, using the `gen_ai` semantic conventions.

To make this real (not a toy), the project ships **AstroMart**, a 4-service FastAPI microshop (gateway → checkout → payment → inventory, plus Postgres and Redis) with a steady load generator and a chaos CLI that injects five distinct failure signatures.

## How I used SigNoz (the whole surface)

This is a SigNoz hackathon, so let me be explicit. SigNoz is both the **agent's data source** and its **observability backend**. Here's every feature and how it's used:

| SigNoz feature | How Agent K uses it |
|---|---|
| **Self-hosted (Foundry)** | The whole SigNoz stack is forged by Foundry from a committed `casting.yaml` + `casting.yaml.lock`. Reproducible: judges can re-run Foundry against the repo. |
| **Traces** | AstroMart's 4 services are OTel-instrumented; the agent's own investigation is a trace (`investigation` root span → `llm.call` and `tool.*` children). |
| **Logs** | Structured JSON logs, deploy-bot markers, and a secret-leak scenario the agent finds via regex over log bodies. |
| **Metrics** | Custom metrics: `agentk.investigations`, `agentk.cost.usd`, `agentk.investigation.duration`, and (new this week) `agentk.audit.groundedness`. |
| **Query Builder v5** | The agent's investigation playbook is a QB v5 cheat-sheet: `countIf(has_error)/count()` error rates, `sumIf(order.total, has_error)` blast radius, `count_distinct(user.id)` impact, group-by `service.version` for rollout comparison, `CONTAINS`/`hasAll` for feature-flag isolation. A dedicated **QB v5 Rubric** dashboard showcases 11 of these. |
| **Alerts** | 8 alert rules provisioned as code — error-rate %, p99 latency, flag-combo, payment timeouts, secret-leak, plus three that watch the agent itself. |
| **Dashboards** | 4 dashboards: AstroMart Overview (RED + business metrics), QB v5 Rubric, **Watch the Watcher** (the agent's own performance/cost/groundedness), and LLM Cost Tracker. |
| **Saved Explorer views** | 6 provisioned views (failing checkouts, slow checkout, secret-leak logs, deploy markers, the agent's LLM calls, its MCP tool calls). |
| **MCP server** | The agent reads _all_ telemetry through the official SigNoz MCP server — 14 curated read-only tools over streamable HTTP. MCP is the **only** tool path; if it's down the investigation fails loudly rather than degrading silently. |
| **gen_ai self-observability** | The agent emits `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.*`, and `llm.cost.usd` on every LLM span — so its reasoning and spend are first-class, queryable SigNoz data. |

The signature idea is that last row. Most "AI + observability" demos point an agent _at_ your telemetry. Agent K also publishes _itself_ into the telemetry. There's a self-investigating alert: `agentk-budget-spike` fires when the agent's own `agentk.cost.usd` rate crosses $2/hour — and when Agent K investigates _that_ alert, **it is investigating itself**, querying its own `llm.call` spans to find which alertname drove the spend.

## The join is where the value is

Neither the client-side symptom ("checkout is erroring") nor the server-side rollup is new on its own. The value is in **correlating them over the exact incident window**. Agent K's playbook anchors every query to the alert's fire window (≈15 min before start → now) so it doesn't drag in older, already-resolved incidents. Then it walks: top-N failing endpoints → error characterization by `status_message` → **deploy correlation by `service.version`** → an exemplar failing trace → logs drill-down → dollar/user blast radius → verdict. That turns "it got slower" into "checkout v1.4.2 introduced a 35% error rate at 14:02, $4,210 in failed orders across 37 users — roll it back."

## Built during the hackathon week: measured, checked, provable

The base agent existed before the event. During the hackathon window I added three things specifically to make the agent **credible** — each independently verifiable by a judge, and each leaning on SigNoz.

### 1. An independent auditor that checks the writer

An LLM writing an RCA can hallucinate a number. So after the writer finishes, a **second, independent model** — its own fresh context, no shared history — screens the report for **groundedness**: is every factual claim, especially every number, backed by the evidence the agent actually collected? It returns a verdict (`grounded` / `ungrounded` + the specific unsupported claims).

It's advisory, not blocking — an ungrounded RCA still ships (an SRE needs the finding) but is **badged** at the top of the report and **counted**. The verdict becomes a `gen_ai`-style `audit.call` span, an `agentk.audit.groundedness` metric (by outcome), a panel on "Watch the Watcher", and an `agentk-ungrounded-rca` alert. The writer is checked, not trusted — and you watch that in SigNoz.

### 2. A hash-chained audit ledger

Every step Agent K takes — investigation start, each tool call (with a hash of the result it saw), each remediation proposal and execution, the audit verdict, the final verdict — is sealed into an **append-only, hash-chained ledger**: `entry_hash = sha256(prev_hash + canonical_payload)`. Any later edit to a row breaks the chain from that point on. `make verify-ledger` recomputes the whole chain and proves it wasn't tampered with. That's the governance guarantee that makes it safe to leave an autonomous agent pointed at production.

### 3. A scored benchmark — because "measured" beats "demoed"

I didn't want to _claim_ the agent finds root causes; I wanted to _measure_ it. `make benchmark` drives the real chaos + a firing webhook for each fault class, lets Agent K run its live investigation, then scores the **stored** verdict **deterministically** against ground truth: detection, localization (right service), classification (right fault signature), remediation (right guarded action), and groundedness. A **healthy control** measures the false-alarm rate — the agent must _not_ page when nothing is wrong. Because scoring keys on the right service + fault tokens + action (not on prose), a run **cannot pass on a hallucinated narrative**. Results land in `docs/benchmark/BENCHMARK.md`, reproducible with one command.

## Guarded autonomy

Autonomy is only safe if it's bounded. Agent K's remediation is allow-listed to three kinds (`rollback`, `disable_flag`, `restart`), each with a data-driven verify step that **re-queries SigNoz** after acting. A "deploy" is modeled as a Redis version flip (no docker-in-docker); only the `restart` action touches Docker, via a read-only socket and a compose-label lookup. Human approval is an HMAC-signed link — no Slack app required. And every investigation is bounded by an iteration cap, a per-investigation cost budget, and a guaranteed-report finish so no run ever ends empty.

## Challenges

- **Window precision.** Correlation only works if the SPL/QB window matches the incident exactly. Anchoring queries to the alert's fire window (and separating historical context from the current firing) was the difference between a right answer and a confidently wrong one.
- **QB v5 span-attribute quirks.** `hasAll` applies to log-body JSON, not span attributes — so feature-flag isolation on spans uses `CONTAINS ... AND CONTAINS ...`. Ambiguous keys like `version` needed qualifying as `attribute.service.version`.
- **Fail loud, not silent.** Early versions had a REST fallback when MCP was unavailable. I removed it: MCP is the only tool path, and if it's down the investigation is marked failed with a clear report. Silent degradation is worse than a loud failure.

## What I learned

Constraining an agent makes it _more_ useful, not less. A tight tool surface, deterministic work-phases, a cost budget, and an independent auditor are what make it safe to trust the output. And the most valuable observability signal is rarely a single metric — it's the **join**: pairing the client-side symptom with the server-side truth over the exact window is what turns telemetry into a root cause.

## Reproduce it

```bash
cp .env.example .env          # add OPENAI_API_KEY (+ optional SLACK_WEBHOOK_URL)
make up                       # SigNoz via committed Foundry manifests + the stack
make bootstrap                # admin + service account + API key → .env
make provision                # 8 alerts + 4 dashboards + 6 views, as code
make demo                     # baseline traffic → bad-deploy incident → Agent K investigates

make benchmark                # scored run: detection/localization/classification + 0% false alarms
make verify-ledger            # prove the hash-chained audit trail is intact
```

Agent K reports: `http://localhost:9000/reports` · SigNoz: `http://localhost:8080` — open **Watch the Watcher** to see the agent observing itself.

---

_Agent K — an AI SRE that brings receipts: a verdict, a business blast radius, a validated fix, an independent groundedness check, and a tamper-evident trail — all grounded in, and observable through, SigNoz._

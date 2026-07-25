# Agent K — Demo Script (3–4 minutes)

Record this flow for the hackathon submission video.

## Setup (before recording)

```bash
cp .env.example .env   # OPENAI_API_KEY + SLACK_WEBHOOK_URL filled in
make up && make bootstrap && make provision
```

Open tabs: SigNoz UI (8080), Agent K reports (9000), Slack channel, **QB v5 Rubric** dashboard.

## Act 1 — The incident (0:00–1:00)

1. Show AstroMart services healthy in SigNoz Services page.
2. Terminal: `make incident-bad-deploy`
3. Cut to Slack — phone buzzes (optional prop).
4. Show checkout error-rate alert firing in SigNoz Alerts (~3 min eval delay — pre-warm if live demo risky).

## Act 2 — Agent K investigates (1:00–2:00)

1. Agent K webhook fires → show agent logs: `make logs-agent`
2. Slack: RCA arrives with blast radius ($ failed orders, users, tenants) + SigNoz deep links.
3. Open `/reports` — show dark monospace report page.
4. Click evidence link → SigNoz trace showing checkout v1.4.2 failure span.

## Act 3 — Deploy Guardian + QB v5 (2:00–2:45)

1. Open **QB v5 Rubric Showcase** dashboard — point at:
   - Rollout compare (v1.4.2 errors)
   - Deploy marker panel (deploy-bot log)
   - sumIf failed order value
2. One line: *"Every query maps to SigNoz's Query Builder v5 rubric."*

## Act 4 — Remediation (2:45–3:15)

1. Slack: click **Approve rollback** link (or `AUTO_APPROVE=true` for drama).
2. Show verification message in Slack after 60s.
3. SigNoz: error rate drops on AstroMart Overview.

## Act 5 — Watch the Watcher (3:15–3:45)

1. Open **Watch the Watcher** dashboard.
2. Show investigation trace in SigNoz (`service.name = agent-k`).
3. Point at gen_ai token attrs + cost footer: *"And here's what it cost: $0.0023"*

## Act 6 — Self-investigation one-liner (3:45–4:00)

1. `make incident-budget` (or show pre-recorded agentk-budget alert).
2. *"SigNoz observes the agent that observes SigNoz — when the budget alert fires, Agent K investigates itself."*

## Hero GIF (for README)

Record a 15s screen capture: Slack RCA notification appearing + split screen with SigNoz trace.
Save as `docs/hero.gif` (use ScreenToGif or Kap on Mac).

## Architecture diagram

See `docs/architecture.md` (Mermaid source) — export PNG for README if needed.

# Agent K — ≤3-minute demo video script

**Goal:** show the closed loop — *the agent debugs via SigNoz **and** is observed by SigNoz.* Live product, not slides. Target **2:45**.

**Before recording:** run `make demo` so traffic + SigNoz data already exist. Keep tabs open: SigNoz (`:8080`), Agent K reports (`:9000/reports`), Slack, terminal. Record at ~1.25× energy; trim dead time in edit.

## Shot list

| Time | Screen | Voiceover |
|---|---|---|
| **0:00–0:20** Hook | You / SigNoz alerts page | "On-call at 3am means pivoting between traces, logs, metrics, and deploys by hand. Agent K is an AI SRE that does that investigation through SigNoz — and is fully observable *in* SigNoz. SigNoz observes the agent that observes SigNoz." |
| **0:20–0:30** Stack | One slide or terminal | "Python, FastAPI, OpenTelemetry, SigNoz self-hosted via Foundry, the official SigNoz MCP server, and an OpenAI-compatible LLM." |
| **0:30–0:50** Trigger | `make incident-bad-deploy` → SigNoz alert firing | "I inject a bad deploy — checkout v1.4.2, 35% errors. A SigNoz alert fires and webhooks the agent." |
| **0:50–1:30** Investigation | Agent K `/reports/<id>` RCA | "The agent investigates over the SigNoz MCP server — aggregating traces, correlating by `service.version`, drilling logs. Root cause: v1.4.2. It quantifies blast radius — failed-order dollars, affected users — with a SigNoz deep link on every claim." |
| **1:30–1:55** Credibility | RCA audit badge → `make verify-ledger` | "Before publishing, a second independent model groundedness-checks the RCA — that's the badge. And every step is sealed to a hash-chained ledger; `make verify-ledger` proves it wasn't tampered with." |
| **1:55–2:15** Remediation | Slack RCA + approve link → verified | "It proposes a guarded rollback behind an HMAC approval link. One click, it executes, then re-queries SigNoz to confirm the error rate dropped." |
| **2:15–2:40** Self-observability | SigNoz **Watch the Watcher** dashboard | "The twist: the agent's own runs live in SigNoz — `gen_ai` spans, cost per investigation, tool calls, groundedness rate. There's even a budget alert where Agent K investigates *itself*." |
| **2:40–2:55** Measured close | `docs/benchmark/BENCHMARK.md` | "Measured, not just demoed — a scored benchmark: detection, localization, 0% false alarms on a healthy control. Reproducible with one command. That's Agent K." |

## Must-cover checklist (from the form)
- [x] About the project (hook)
- [x] Tech stack & architecture (0:20 line)
- [x] Demo (incident → RCA → remediation → self-observability)
- [x] Learning/growth (optional) — add one line if time: "Constraining the agent — one tool surface, a cost budget, an independent auditor — is what made it safe to trust."

## Tips
- Caption the closed-loop moment on screen — it's the differentiator.
- Keep it under 3:00 hard; judges may stop watching past the limit.

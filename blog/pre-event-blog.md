# I Self-Hosted SigNoz for a Hackathon — Here's What the Docs Don't Cover

> Publish on Dev.to / Medium. Screenshot placeholders marked `[SCREENSHOT: ...]` — take from local SigNoz at http://localhost:8080 before publishing. Cover image: `blog/devto-cover.png`. ~1,300 words.

---

I sat down on a Saturday to self-host SigNoz for the Agents of SigNoz hackathon. The [official Docker guide](https://signoz.io/docs/install/docker/) is clear: install `foundryctl`, write a `casting.yaml`, run `foundryctl cast`. Three commands, containers up, UI on `:8080`. Done.

Except not done. The docs get SigNoz running. They don't get a four-service app *into* it, don't mention that telemetry sits idle until first-boot setup finishes, and don't cover provisioning alerts and dashboards through the API. Stack Overflow and older blog posts still point at `./install.sh` in the SigNoz repo's `deploy/` folder — that path was [deprecated in v0.130.0](https://github.com/SigNoz/signoz/releases/tag/v0.130.0). If you land there first, you'll think Docker is broken. It isn't; the old bundled compose is just gone.

This post is the map I wish I'd had after `docker ps` looked healthy: Foundry with a multi-service Docker network, wiring OTLP from real apps, and alerts/dashboards as code — plus three gotchas that cost me real hours.

## Foundry: what the install guide actually gives you

Foundry is SigNoz's install-as-code tool. You declare what you want in `casting.yaml`; Foundry renders Docker artifacts and applies them. The happy path from the docs:

```bash
curl -fsSL https://signoz.io/foundry.sh | bash   # install foundryctl
```

```yaml
# casting.yaml
apiVersion: v1alpha1
kind: Installation
metadata:
  name: signoz
spec:
  deployment:
    flavor: compose
    mode: docker
```

```bash
foundryctl cast -f casting.yaml   # gauge → forge → start containers
```

`cast` writes a `pours/deployment/` directory and brings the stack up. Inside you'll find a plain, readable `compose.yaml` — ClickHouse, ClickHouse Keeper, Postgres metastore, the SigNoz server, and the OTel ingester. That's the part I like most: generated infrastructure you can commit to git, diff on upgrades, and run yourself with `docker compose up -d` if you prefer manual control (`foundryctl forge` renders without starting).

One wrinkle when wiring other containers into the same network: the generated collector DNS alias is `signoz-ingester`, while every OpenTelemetry tutorial (and my app configs) expect `signoz-otel-collector`. Foundry lets you patch the rendered compose declaratively instead of hand-editing `pours/`:

```yaml
  patches:
    - target: "deployment/compose.yaml"
      operations:
        - op: add
          path: /services/ingester/networks/signoz-network/aliases/-
          value: signoz-otel-collector
        - op: replace
          path: /services/signoz-signoz-0/networks
          value:
            signoz-network:
              aliases:
                - signoz
```

Re-run `cast` (or `forge` if you're managing compose yourself), and both hostnames resolve. My four FastAPI services (gateway → checkout → payment → inventory, plus a load generator) point `OTEL_EXPORTER_OTLP_ENDPOINT` at `http://signoz-otel-collector:4317` and never know anything changed.

**Gotcha #1: no telemetry flows until you finish first-boot setup.** My services were up, exporting OTLP, zero errors — and ClickHouse stayed empty. The collector's logs showed OpAMP connection errors. Turns out the ingester gets its live config from the SigNoz server over OpAMP, and that only starts working after the initial admin account is created (`/api/v1/version` shows `"setupCompleted": false` until then). Create the account, and within a minute spans started landing. If you're staring at an empty Services page with healthy containers, check this first.

[SCREENSHOT: Services page showing gateway/checkout/payment/inventory with latency + error rate columns]

## My favorite feature: Query Builder v5 is a real query language now

The thing that made me stay up late wasn't the install — it was the new **v5 filter expressions**. Filters are no longer stacks of key/operator/value dropdowns; they're strings you type, like SQL's WHERE clause but attribute-aware:

```
service.name = 'checkout' AND has_error = true
```

Aggregations are expressions too — `count()`, `p99(duration_nano)`, `count_distinct(user.id)`. The combination that sold me completely — comparing a rollout by grouping error counts by `service.version`:

```
count()  where service.name = 'checkout' AND has_error = true
         group by service.version
```

During a simulated bad deploy, this splits cleanly: v1.4.2 carrying all the errors, v1.4.1 flat at zero. One query answers "did the deploy break it?" (The API layer goes further than the explorer dropdowns expose — alert rules accept conditional aggregation expressions like `countIf(has_error = true)` directly, which I use below.)

[SCREENSHOT: Traces explorer, group by service.version, showing errors concentrated on 1.4.2]

Log search gets the same language, including regex. This finds AWS-style access keys leaking into production logs:

```
body REGEXP 'AKIA[0-9A-Z]{16}' AND deployment.environment = 'production'
```

I turned exactly that expression into an alert rule — a secret-leak tripwire in one line.

**Gotcha #2: resource attributes shadow span attributes in group-bys.** My checkout service models deploys dynamically — the "deployed version" changes at runtime without a container restart, stamped on each span as a `service.version` span attribute. But my group-by kept showing the *old* version for every span, errors included. The reason: I also had `service.version=1.4.1` baked into `OTEL_RESOURCE_ATTRIBUTES`, and when both a resource attribute and a span attribute share a name, the resource wins in queries. The fix: pick one owner for the attribute. I removed it from the resource env and stamped it per-request in an ASGI middleware:

```python
@app.middleware("http")
async def stamp_service_version(request, call_next):
    span = trace.get_current_span()
    if span and span.is_recording():
        span.set_attribute("service.version", await current_version())
    return await call_next(request)
```

Even after the fix there's a second layer: if a key has *ever* existed in both contexts, SigNoz resolves the bare name to the **resource** context by default — my group-by silently returned empty versions until I read the warning attached to the query response: *"Key 'service.version' is ambiguous… Using 'resource' context by default. To query attributes explicitly, use the fully qualified name (e.g., 'attribute.service.version')."* Qualified name in the group-by, and rollout comparison worked exactly as advertised. Read the warnings SigNoz attaches to results — every one I hit contained the exact fix.

## Alerts and dashboards as code (against the real API)

I wanted the whole setup reproducible from a fresh clone, so no clicking around the UI: notification channels, five alert rules, and three dashboards all provisioned by a Python script hitting the REST API. Three things I learned that aren't in any tutorial yet:

**Alert rules want the v5 shape.** My first attempt used the older `builderQueries` JSON — rejected with `"alert rule is not valid"`. The current schema mirrors what the UI sends: `"version": "v5"` and a `queries` array where each entry has a `spec` with `signal`, `aggregations` (expression strings), and a `filter` expression:

```json
{
  "alert": "checkout-error-rate",
  "alertType": "TRACES_BASED_ALERT",
  "ruleType": "threshold_rule",
  "version": "v5",
  "evalWindow": "1m",
  "condition": {
    "compositeQuery": {
      "queryType": "builder",
      "queries": [{
        "type": "builder_query",
        "spec": {
          "name": "A",
          "signal": "traces",
          "aggregations": [{"expression": "countIf(has_error = true)"}],
          "filter": {"expression": "service.name = 'checkout'"}
        }
      }]
    },
    "op": "above", "target": 10, "matchType": "at_least_once"
  },
  "preferredChannels": ["agent-k-webhook"]
}
```

Note `preferredChannels` takes channel *names*, not IDs — the server folds them into the rule's notification thresholds itself. And webhook channels follow the upstream Alertmanager schema: the key is `url`, not `api_url` (the error message `one of url or url_file must be configured` finally tipped me off).

**Gotcha #3: `POST /api/v1/dashboards` wants the dashboard content directly.** I exported a dashboard, saved it as `{"data": {...}}`, and POSTed the whole file. The API happily returned success — and stored a broken, title-less dashboard with my payload double-nested inside. No error, just quiet corruption. Unwrap the file and send the inner object (title, widgets, …). If your dashboard list ever shows untitled entries, this is why.

The payoff: `make provision` takes a virgin SigNoz to fully-armed — channels, alerts, dashboards — in about four seconds, idempotently. Trigger a bad deploy in the sandbox and the checkout-error-rate alert fires and webhooks out about three minutes later.

Why three minutes and not one? I stared at "inactive" rules long enough to go read the server logs: every evaluation logs `eval_delay: 120000`. Threshold rules deliberately evaluate a window ending **two minutes in the past**, so late-arriving spans can't cause false negatives — my 1-minute window at 11:08 was scoring traffic from 11:05–11:06, before the incident started. Once you know that, "my alert is slow" becomes "my alert is correct"; budget for `eval window + 2m` in any latency math you do.

[SCREENSHOT: Alerts page with the five provisioned rules, checkout-error-rate in FIRING state]

[SCREENSHOT: one provisioned dashboard with panels populated]

## What I'd tell my past self

- **Follow the official path, then go further.** Foundry's generated `compose.yaml` is readable and committable — `pours/` in git beats a hand-maintained compose file.
- **Empty telemetry ≠ broken exporter.** Check `setupCompleted` and the OpAMP logs before touching your app.
- **One owner per attribute.** Resource attrs are for static facts; anything dynamic belongs on spans — and never both.
- **The API errors are good.** Every schema mistake I made produced a message that, read carefully, contained the fix.
- **Expressions > dropdowns.** v5 filter syntax is the first query builder I've used where the "builder" part never got in my way.

Everything above ran on my machine this week — SigNoz v0.133, Foundry v0.2.14, Python 3.12 services on Docker. Next week is the Agents of SigNoz hackathon; this stack is the foundation I'll be building on, and now you can stand it up in an afternoon instead of a weekend.

*Docs: [signoz.io/docs/install/docker](https://signoz.io/docs/install/docker/) · Foundry: [github.com/SigNoz/foundry](https://github.com/SigNoz/foundry)*

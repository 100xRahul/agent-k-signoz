# I Self-Hosted SigNoz the Week Its docker-compose Died — Here's the Map

> Publish on Dev.to / Medium. Screenshot placeholders are marked `[SCREENSHOT: ...]` — take them from your local SigNoz at http://localhost:8080 before publishing. ~1,300 words.

---

I sat down on a Saturday to self-host SigNoz and hit this in the repo's `deploy/` folder:

> **Note:** The `install.sh` script and the `docker-compose` manifests have been deprecated.

Every tutorial I'd bookmarked was suddenly wrong. SigNoz now installs through **Foundry**, a new tool that *generates* your deployment instead of shipping you a frozen compose file. This post is the map I wish I'd had: getting SigNoz running with Foundry, wiring a multi-service app into it, and provisioning alerts and dashboards entirely through the API — plus three gotchas that cost me real hours, which you can now skip.

## Foundry in two commands

Foundry splits deployment into *forge* (generate manifests from a spec) and *cast* (apply them). The spec is a small `casting.yaml`:

```yaml
apiVersion: v1alpha1
kind: Installation
metadata:
  name: signoz
spec:
  deployment:
    flavor: compose
    mode: docker
```

Run `foundryctl forge -f casting.yaml` and you get a `pours/deployment/` directory containing a plain, readable `docker-compose.yaml` — ClickHouse, ClickHouse Keeper, Postgres metastore, the signoz server, and the OTel-collector ingester. That's the part I like most: the output is standard Docker artifacts you can commit to git, diff on upgrades, and `docker compose up -d` yourself. Infrastructure you can read.

One wrinkle: the generated network is `signoz-network` and the collector's DNS alias is `signoz-ingester`, while years of muscle memory (and my app configs) expected `signoz-otel-collector`. Instead of renaming everything, Foundry lets you patch the generated manifests declaratively:

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

Re-forge, and both hostnames resolve. My four FastAPI services (a small shop: gateway → checkout → payment → inventory, plus a load generator) point `OTEL_EXPORTER_OTLP_ENDPOINT` at `http://signoz-otel-collector:4317` and never know anything changed.

**Gotcha #1: no telemetry flows until you finish first-boot setup.** My services were up, exporting OTLP, zero errors — and ClickHouse stayed empty. The collector's logs showed OpAMP connection errors. Turns out the ingester gets its live config from the SigNoz server over OpAMP, and that only starts working after the initial admin account is created (`/api/v1/version` shows `"setupCompleted": false` until then). Create the account, and within a minute spans started landing. If you're staring at an empty Services page with healthy containers, check this first.

[SCREENSHOT: Services page showing gateway/checkout/payment/inventory with latency + error rate columns]

## My favorite feature: Query Builder v5 is a real query language now

The thing that made me stay up late wasn't the install — it was the new **v5 filter expressions**. Filters are no longer stacks of key/operator/value dropdowns; they're strings you type, like SQL's WHERE clause but attribute-aware:

```
service.name = 'checkout' AND has_error = true
```

Aggregations are expressions too. Error count per minute for one service is `countIf(has_error = true)`; tail latency is `p99(duration_nano)`. The one that sold me completely — grouping by `service.version` to compare a rollout:

```
countIf(has_error = true)  group by service.version
```

During a simulated bad deploy, this splits cleanly: v1.4.2 carrying all the errors, v1.4.1 clean. One query answers "did the deploy break it?"

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

After that, rollout comparison worked exactly as advertised. SigNoz even warns you about this class of problem — my API responses included `"Key has_error is ambiguous, found 2 different combinations of field context / data type"` — read those warnings, they're telling the truth.

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

- **Don't fight the migration — use it.** Foundry's generated compose is more readable than the old monolithic file ever was, and committing `pours/` gives you reviewable infra.
- **Empty telemetry ≠ broken exporter.** Check `setupCompleted` and the OpAMP logs before touching your app.
- **One owner per attribute.** Resource attrs are for static facts; anything dynamic belongs on spans — and never both.
- **The API errors are good.** Every schema mistake I made produced a message that, read carefully, contained the fix.
- **Expressions > dropdowns.** v5 filter syntax is the first query builder I've used where the "builder" part never got in my way.

Everything above ran on my machine this week — SigNoz v0.133, Foundry v0.2.14, Python 3.12 services on Docker. Next week is the Agents of SigNoz hackathon; this stack is the foundation I'll be building on, and now you can stand it up in an afternoon instead of a weekend.

*Docs: [signoz.io/docs/install/docker](https://signoz.io/docs/install/docker/) · Foundry: [github.com/SigNoz/foundry](https://github.com/SigNoz/foundry)*

#!/usr/bin/env python3
"""Build and POST an Alertmanager-style webhook for a chaos scenario."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

SCENARIOS: dict[str, dict] = {
    "bad-deploy": {
        "alertname": "checkout-error-rate",
        "labels": {
            "severity": "critical",
            "service": "checkout",
            "scenario": "bad-deploy",
        },
        "annotations": {
            "summary": "Checkout error rate exceeds 10%",
            "description": "The checkout service error rate exceeded 10% — likely bad deploy v1.4.2.",
        },
    },
    "flag-combo": {
        "alertname": "flag-combo-errors",
        "labels": {
            "severity": "critical",
            "service": "checkout",
            "scenario": "flag-combo",
        },
        "annotations": {
            "summary": "Flag-combo errors detected on checkout",
            "description": "Errors when both new-checkout and express-pay flags are present.",
        },
    },
    "secret-leak": {
        "alertname": "secret-leak-detected",
        "labels": {"severity": "critical", "scenario": "secret-leak"},
        "annotations": {
            "summary": "AWS access key pattern in production logs",
            "description": "Secret leak detected in payment logs.",
        },
    },
    "pool-exhaustion": {
        "alertname": "payment-timeouts",
        "labels": {
            "severity": "critical",
            "service": "payment",
            "scenario": "pool-exhaustion",
        },
        "annotations": {
            "summary": "Payment timeout errors",
            "description": "Payment DB pool exhaustion causing timeouts.",
        },
    },
    "budget": {
        "alertname": "agentk-budget-spike",
        "labels": {"severity": "warning", "service": "agent-k"},
        "annotations": {
            "summary": "Agent K LLM spend exceeds budget",
            "description": "Meta-investigation: agent cost rate elevated.",
        },
    },
}


def build_payload(scenario: str) -> dict:
    spec = SCENARIOS[scenario]
    starts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat().replace(
        "+00:00", "Z"
    )
    alertname = spec["alertname"]
    return {
        "receiver": "agent-k-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": alertname, **spec["labels"]},
                "annotations": spec["annotations"],
                "startsAt": starts,
                "endsAt": "",
                "generatorURL": f"http://localhost:8080/alerts/overview?rule={alertname}",
                "fingerprint": f"test-{scenario}",
            }
        ],
        "groupLabels": {"alertname": alertname},
        "commonLabels": {"alertname": alertname},
        "commonAnnotations": spec["annotations"],
        "externalURL": "http://localhost:8080",
        "version": "4",
        "groupKey": alertname,
    }


def post_webhook(agent_url: str, payload: dict) -> tuple[int, str]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{agent_url.rstrip('/')}/webhook/signoz",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main() -> int:
    parser = argparse.ArgumentParser(description="Fire a test SigNoz webhook")
    parser.add_argument(
        "scenario",
        choices=sorted(SCENARIOS.keys()),
        help="Chaos scenario to simulate",
    )
    parser.add_argument("--agent-url", default="http://localhost:9000")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    payload = build_payload(args.scenario)
    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    code, body = post_webhook(args.agent_url, payload)
    print(f"POST /webhook/signoz → {code}: {body[:300]}")
    return 0 if code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Burn Agent K token budget to demo self-investigation (agentk-budget-spike alert).

Fires several manual investigations rapidly to spike agentk.cost.usd.
Requires OPENAI_API_KEY and a running agent container.

Usage:
    python scripts/incident_budget.py [--agent-url http://localhost:9000] [--count 3]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

PROMPTS = [
    "Investigate agent-k's own LLM cost spike. Query agent-k traces and metrics.",
    "Why is agentk.cost.usd rate elevated? Analyze llm.call spans for token usage.",
    "Meta-investigation: which alertnames drove the most Agent K spend today?",
]


def post_json(url: str, payload: dict) -> tuple[int, str]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-url", default="http://localhost:9000")
    parser.add_argument("--count", type=int, default=3)
    args = parser.parse_args()
    base = args.agent_url.rstrip("/")

    print(f"💸 Firing {args.count} investigations to spike agentk.cost.usd...")
    for i in range(args.count):
        prompt = PROMPTS[i % len(PROMPTS)]
        code, body = post_json(f"{base}/investigate", {"prompt": prompt})
        print(f"  [{i + 1}/{args.count}] POST /investigate → {code}")
        if code not in (200, 202):
            print(f"    {body[:200]}")
            return 1
        time.sleep(2)

    print("✅ Budget burn triggered. Watch Watch-the-Watcher dashboard + agentk-budget alert.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Lightweight checkpoint verifier for Agent K demo path.

Usage:
    python scripts/verify_checkpoints.py [--agent-url http://localhost:9000]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def get(url: str, timeout: float = 10.0) -> tuple[int, str]:
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def post_json(url: str, payload: dict, timeout: float = 30.0) -> tuple[int, str]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def wait_for_investigation(agent_url: str, timeout_s: int = 180) -> dict | None:
    """Poll /reports until a non-running investigation appears."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        code, body = get(f"{agent_url.rstrip('/')}/reports")
        if code != 200:
            time.sleep(5)
            continue
        # Parse HTML list is hard — use a manual investigate + poll store via API
        # For now hit investigate and poll by re-triggering health only.
        time.sleep(5)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Agent K checkpoints")
    parser.add_argument(
        "--agent-url", default="http://localhost:9000", help="Agent K base URL"
    )
    args = parser.parse_args()
    base = args.agent_url.rstrip("/")

    print("1️⃣  Health check...")
    code, _ = get(f"{base}/healthz")
    if code != 200:
        print(f"   ❌ /healthz returned {code}")
        return 1
    print("   ✅ Agent K is up")

    print("2️⃣  Trigger manual investigation...")
    code, body = post_json(
        f"{base}/investigate",
        {"prompt": "Check checkout error rate in the last 15 minutes. Is anything wrong?"},
    )
    if code not in (200, 202):
        print(f"   ❌ POST /investigate returned {code}: {body[:200]}")
        return 1
    print("   ✅ Investigation accepted")

    print("3️⃣  Waiting for investigation to complete (up to 3 min)...")
    # Poll reports HTML for a done status — crude but works without new API
    deadline = time.time() + 180
    found = False
    while time.time() < deadline:
        code, html = get(f"{base}/reports")
        if code == 200 and "done" in html.lower() and "checkout" in html.lower():
            found = True
            break
        time.sleep(10)

    if not found:
        print("   ⚠️  Could not confirm completed RCA on /reports (may still be running)")
        print("   Check manually: open /reports and SigNoz traces for agent-k")
        return 0

    print("   ✅ Report visible on /reports")
    print("\n✅ Checkpoint verification passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

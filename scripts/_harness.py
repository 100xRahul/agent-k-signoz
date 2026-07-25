#!/usr/bin/env python3
"""Shared live-run plumbing for the Agent K harnesses (test_live, benchmark).

Small, dependency-free HTTP + subprocess helpers plus the investigation-polling
loop, factored out so `test_live.py` and `benchmark.py` share one implementation.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def get(url: str, timeout: float = 15.0) -> tuple[int, str]:
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def post_json(url: str, payload: dict, timeout: float = 30.0) -> tuple[int, str]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def run_make(target: str) -> int:
    return subprocess.call(["make", target], cwd=ROOT)


def fire_webhook(scenario: str) -> int:
    return subprocess.call(
        [sys.executable, str(ROOT / "scripts" / "fire_webhook.py"), scenario],
        cwd=ROOT,
    )


def wait_investigation(
    agent_url: str,
    timeout_s: int = 240,
    predicate=None,
    after_ts: float | None = None,
) -> dict | None:
    """Poll /api/investigations until one matching predicate completes.

    Returns the full investigation dict (including report_md, audit, actions).
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        code, body = get(f"{agent_url.rstrip('/')}/api/investigations?limit=20")
        if code == 200:
            data = json.loads(body)
            for inv in data.get("investigations", []):
                if inv.get("status") == "running":
                    continue
                if after_ts and inv.get("started_at"):
                    try:
                        started = datetime.fromisoformat(
                            inv["started_at"].replace("Z", "+00:00")
                        ).timestamp()
                        if started < after_ts:
                            continue
                    except ValueError:
                        pass
                if predicate and not predicate(inv):
                    continue
                if inv.get("status") in ("done", "failed"):
                    return inv
        time.sleep(5)
    return None

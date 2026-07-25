#!/usr/bin/env python3
"""Live E2E test suite for Agent K (requires running stack + OPENAI_API_KEY)."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import (  # noqa: E402
    ROOT,
    fire_webhook,
    get,
    post_json,
    run_make,
    wait_investigation,
)

LOG_PATH = ROOT / "docs" / "TEST_LOG.md"


def log(msg: str, lines: list[str]) -> None:
    print(msg)
    lines.append(msg)


def append_test_log(lines: list[str], passed: bool) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    header = f"\n## Live test run — {datetime.now(timezone.utc).isoformat()}\n\n"
    status = "**PASS**" if passed else "**FAIL**"
    body = header + f"Result: {status}\n\n" + "\n".join(f"- {line}" for line in lines) + "\n"
    if LOG_PATH.exists():
        existing = LOG_PATH.read_text()
        LOG_PATH.write_text(existing + body)
    else:
        LOG_PATH.write_text("# Agent K — Live Test Log\n" + body)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-url", default="http://localhost:9000")
    parser.add_argument("--skip-chaos", action="store_true")
    args = parser.parse_args()
    base = args.agent_url.rstrip("/")
    lines: list[str] = []
    ok = True

    for name, url in [
        ("SigNoz", "http://localhost:8080/api/v1/health"),
        ("Agent", f"{base}/healthz"),
    ]:
        code, _ = get(url)
        if code != 200:
            log(f"FAIL health {name} ({code})", lines)
            ok = False
        else:
            log(f"PASS health {name}", lines)

    if not ok:
        append_test_log(lines, False)
        return 1

    manual_start = time.time()
    code, _ = post_json(
        f"{base}/investigate",
        {"prompt": "List services and summarize checkout health in the last 15 minutes."},
    )
    if code not in (200, 202):
        log(f"FAIL manual investigate HTTP {code}", lines)
        append_test_log(lines, False)
        return 1

    inv = wait_investigation(
        base,
        timeout_s=300,
        after_ts=manual_start - 5,
        predicate=lambda i: "checkout" in (i.get("report_md") or "").lower()
        or "checkout" in (i.get("root_cause") or "").lower(),
    )
    if not inv or inv.get("status") != "done":
        log("FAIL manual investigation did not complete", lines)
        append_test_log(lines, False)
        return 1
    log(
        f"PASS manual investigation {inv['id']} cost=${inv.get('cost_usd', 0):.4f}",
        lines,
    )

    if args.skip_chaos:
        append_test_log(lines, True)
        return 0

    if run_make("resolve") != 0:
        log("WARN resolve before bad-deploy failed", lines)
    time.sleep(3)
    if run_make("incident-bad-deploy") != 0:
        log("FAIL make incident-bad-deploy", lines)
        append_test_log(lines, False)
        return 1
    time.sleep(5)
    bad_start = time.time()
    if fire_webhook("bad-deploy") != 0:
        log("FAIL fire_webhook bad-deploy", lines)
        ok = False
    inv = wait_investigation(
        base,
        timeout_s=360,
        after_ts=bad_start - 5,
        predicate=lambda i: i.get("alertname") == "checkout-error-rate"
        or "1.4.2" in (i.get("root_cause") or "")
        or "bad-deploy" in (i.get("root_cause") or "").lower(),
    )
    if not inv:
        log("FAIL bad-deploy investigation timeout", lines)
        ok = False
    else:
        rc = ((inv.get("root_cause") or "") + (inv.get("report_md") or "")).lower()
        if any(x in rc for x in ("1.4.2", "bad-deploy", "bad deploy", "v1.4.2")):
            log(f"PASS bad-deploy RCA {inv['id']}", lines)
        else:
            log(f"FAIL bad-deploy RCA missing deploy root cause: {inv.get('root_cause')}", lines)
            ok = False

    run_make("resolve")
    time.sleep(3)
    run_make("incident-flags")
    time.sleep(5)
    flag_start = time.time()
    fire_webhook("flag-combo")
    inv = wait_investigation(
        base,
        timeout_s=360,
        after_ts=flag_start - 5,
        predicate=lambda i: i.get("alertname") == "flag-combo-errors"
        or "express-pay" in (i.get("root_cause") or "").lower(),
    )
    if not inv:
        log("FAIL flag-combo investigation timeout", lines)
        ok = False
    else:
        rc = ((inv.get("root_cause") or "") + (inv.get("report_md") or "")).lower()
        if any(x in rc for x in ("express-pay", "new-checkout", "flag", "hasall")):
            log(f"PASS flag-combo RCA {inv['id']}", lines)
        else:
            log(f"FAIL flag-combo RCA: {inv.get('root_cause')}", lines)
            ok = False

    append_test_log(lines, ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

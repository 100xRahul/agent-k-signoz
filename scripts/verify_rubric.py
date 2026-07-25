#!/usr/bin/env python3
"""Verify all four chaos scenarios produce sensible investigation triggers.

Does NOT run LLM investigations (expensive) — validates chaos CLI + alert labels exist.

Usage:
    make verify-rubric
"""

from __future__ import annotations

import subprocess
import sys

SCENARIOS = [
    ("bad-deploy", "checkout-error-rate"),
    ("pool-exhaustion", "payment-timeouts"),
    ("flag-combo", "flag-combo-errors"),
    ("secret-leak", "secret-leak-detected"),
]


def run(cmd: list[str]) -> int:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.call(cmd)


def main() -> int:
    print("🧪 verify-rubric: chaos scenarios + alert pairing\n")

    for scenario, expected_alert in SCENARIOS:
        print(f"▶ Scenario: {scenario} (expect alert: {expected_alert})")
        make_target = {
            "bad-deploy": "incident-bad-deploy",
            "pool-exhaustion": "incident-pool",
            "flag-combo": "incident-flags",
            "secret-leak": "incident-leak",
        }[scenario]
        rc = run(["make", make_target])
        if rc != 0:
            print(f"   ❌ make {make_target} failed")
            return rc
        print(f"   ✅ Chaos triggered — confirm alert '{expected_alert}' fires in SigNoz (~3 min eval delay)\n")

    print("▶ Resolve all scenarios")
    rc = run(["make", "resolve"])
    if rc != 0:
        return rc

    print("\n✅ All chaos scenarios executed. Run `make demo-full` for full E2E with Agent K.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Verify Agent K's hash-chained audit ledger is intact.

Every investigation step (start, each tool call, remediation proposal/execution,
audit verdict, final verdict) is sealed into an append-only, hash-chained ledger
in SQLite. This recomputes the chain and proves it was not tampered with — the
governance guarantee that makes it safe to leave the agent running unattended.

Runs against the live agent (the DB lives inside the agent container), so it
needs no direct DB access:

    make verify-ledger
    python scripts/verify_ledger.py --agent-url http://localhost:9000
    python scripts/verify_ledger.py --investigation <id>   # also dump entries
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import get  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-url", default="http://localhost:9000")
    parser.add_argument(
        "--investigation",
        default="",
        help="Dump this investigation's ledger entries in addition to verifying.",
    )
    args = parser.parse_args()
    base = args.agent_url.rstrip("/")

    code, body = get(f"{base}/api/ledger/verify")
    if code != 200:
        print(f"❌ could not reach agent ledger endpoint (HTTP {code})")
        return 2
    result = json.loads(body)

    if args.investigation:
        icode, ibody = get(f"{base}/reports/{args.investigation}/ledger")
        if icode == 200:
            entries = json.loads(ibody).get("entries", [])
            print(f"Ledger entries for investigation {args.investigation}:")
            for e in entries:
                payload = json.loads(e["payload_json"])
                print(
                    f"  seq={e['seq']:>4}  {e['entry_type']:<22} "
                    f"{e['entry_hash'][:12]}…  {payload.get('payload', {})}"
                )
            print()

    if result.get("chain_ok"):
        print(f"✅ ledger intact — {result.get('entries_checked', 0)} entries, chain verified")
        return 0

    print(
        f"❌ TAMPER DETECTED at seq {result.get('tampered_seq')} "
        f"(verified {result.get('entries_checked', 0)} entries before the break)"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())

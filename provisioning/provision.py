"""
Agent K — Provisioning Script

Idempotent provisioning of dashboards, alert rules, and notification channels
into SigNoz via REST API (or MCP tools).

Usage:
    python -m provisioning.provision
"""

import json
import os
import httpx
import time
from pathlib import Path

SIGNOZ_URL = os.environ.get("SIGNOZ_INTERNAL_URL", os.environ.get("SIGNOZ_URL", "http://signoz:8080"))
SIGNOZ_API_KEY = os.environ.get("SIGNOZ_API_KEY", "")
AGENT_WEBHOOK_URL = os.environ.get("AGENT_WEBHOOK_URL", "http://agent:9000/webhook/signoz")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

HEADERS = {
    "Content-Type": "application/json",
    "SIGNOZ-API-KEY": SIGNOZ_API_KEY,
}

PROVISIONING_DIR = Path(__file__).parent


def api(method: str, path: str, body: dict | None = None) -> dict:
    """Make an API call to SigNoz."""
    url = f"{SIGNOZ_URL}/api/v1{path}"
    resp = httpx.request(method, url, json=body, headers=HEADERS, timeout=30.0)
    if resp.status_code not in (200, 201):
        print(f"  ⚠️  {method} {path} → {resp.status_code}: {resp.text[:200]}")
        return {}
    try:
        return resp.json()
    except Exception:
        return {}


def provision_notification_channels() -> dict[str, int]:
    """Create or find notification channels. Returns name→id mapping."""
    print("📡 Provisioning notification channels...")

    existing = api("GET", "/channels")
    channel_map: dict[str, int] = {}

    if existing and "data" in existing:
        for ch in existing["data"]:
            channel_map[ch["name"]] = ch["id"]

    # Agent K webhook channel
    if "agent-k-webhook" not in channel_map:
        result = api("POST", "/channels", {
            "name": "agent-k-webhook",
            "type": "webhook",
            "webhook_configs": [{
                "api_url": AGENT_WEBHOOK_URL,
                "send_resolved": True,
            }],
        })
        if result and "data" in result:
            channel_map["agent-k-webhook"] = result["data"]["id"]
            print("  ✅ Created agent-k-webhook channel")
        else:
            print("  ⚠️  Failed to create agent-k-webhook channel")
    else:
        print("  ✔️  agent-k-webhook channel already exists")

    # Slack channel (if configured)
    if SLACK_WEBHOOK_URL and "agent-k-slack" not in channel_map:
        result = api("POST", "/channels", {
            "name": "agent-k-slack",
            "type": "slack",
            "slack_configs": [{
                "api_url": SLACK_WEBHOOK_URL,
                "send_resolved": True,
                "channel": "#agent-k",
            }],
        })
        if result and "data" in result:
            channel_map["agent-k-slack"] = result["data"]["id"]
            print("  ✅ Created agent-k-slack channel")
        else:
            print("  ⚠️  Failed to create agent-k-slack channel")
    elif SLACK_WEBHOOK_URL:
        print("  ✔️  agent-k-slack channel already exists")

    return channel_map


def provision_alerts(channel_ids: list[int]) -> None:
    """Provision alert rules from JSON files."""
    print("\n🚨 Provisioning alert rules...")

    alerts_dir = PROVISIONING_DIR / "alerts"
    if not alerts_dir.exists():
        print("  ⚠️  No alerts directory found")
        return

    existing = api("GET", "/rules")
    existing_names: set[str] = set()
    if existing and "data" in existing and "rules" in existing["data"]:
        for rule in existing["data"]["rules"]:
            existing_names.add(rule.get("alert", ""))

    for alert_file in sorted(alerts_dir.glob("*.json")):
        with open(alert_file) as f:
            alert_def = json.load(f)

        alert_name = alert_def.get("alert", alert_file.stem)

        # Inject notification channel IDs
        if "preferredChannels" not in alert_def:
            alert_def["preferredChannels"] = []
        for ch_id in channel_ids:
            if ch_id not in alert_def["preferredChannels"]:
                alert_def["preferredChannels"].append(str(ch_id))

        if alert_name in existing_names:
            print(f"  ✔️  Alert '{alert_name}' already exists")
        else:
            result = api("POST", "/rules", alert_def)
            if result:
                print(f"  ✅ Created alert '{alert_name}'")
            else:
                print(f"  ⚠️  Failed to create alert '{alert_name}'")


def provision_dashboards() -> None:
    """Provision dashboards from JSON files."""
    print("\n📊 Provisioning dashboards...")

    dashboards_dir = PROVISIONING_DIR / "dashboards"
    if not dashboards_dir.exists():
        print("  ⚠️  No dashboards directory found")
        return

    existing = api("GET", "/dashboards")
    existing_titles: set[str] = set()
    if existing and "data" in existing:
        for dash in existing["data"]:
            title = dash.get("data", {}).get("title", "")
            if title:
                existing_titles.add(title)

    for dash_file in sorted(dashboards_dir.glob("*.json")):
        with open(dash_file) as f:
            dash_def = json.load(f)

        title = dash_def.get("data", {}).get("title", dash_file.stem)

        if title in existing_titles:
            print(f"  ✔️  Dashboard '{title}' already exists")
        else:
            result = api("POST", "/dashboards", dash_def)
            if result:
                print(f"  ✅ Created dashboard '{title}'")
            else:
                print(f"  ⚠️  Failed to create dashboard '{title}'")


def main() -> None:
    print("🕶️  Agent K — Provisioning\n")
    print(f"SigNoz URL: {SIGNOZ_URL}")
    print(f"API Key: {'configured' if SIGNOZ_API_KEY else '⚠️  NOT SET'}")
    print()

    # Wait for SigNoz to be ready
    for attempt in range(10):
        try:
            resp = httpx.get(f"{SIGNOZ_URL}/api/v1/health", timeout=5.0)
            if resp.status_code == 200:
                print("✅ SigNoz is ready\n")
                break
        except Exception:
            pass
        print(f"⏳ Waiting for SigNoz... (attempt {attempt + 1}/10)")
        time.sleep(5)
    else:
        print("❌ SigNoz not reachable. Provisioning may fail.\n")

    channel_map = provision_notification_channels()
    channel_ids = list(channel_map.values())

    provision_alerts(channel_ids)
    provision_dashboards()

    print("\n✅ Provisioning complete!")


if __name__ == "__main__":
    main()

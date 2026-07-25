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

SIGNOZ_URL = os.environ.get(
    "SIGNOZ_INTERNAL_URL", os.environ.get("SIGNOZ_URL", "http://signoz:8080")
)
SIGNOZ_API_KEY = os.environ.get("SIGNOZ_API_KEY", "")
AGENT_WEBHOOK_URL = os.environ.get(
    "AGENT_WEBHOOK_URL", "http://agent:9000/webhook/signoz"
)
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


def provision_notification_channels() -> list[str]:
    """Create or find notification channels. Returns channel names (alert rules
    reference channels by name, not id)."""
    print("📡 Provisioning notification channels...")

    existing = api("GET", "/channels")
    existing_names: set[str] = set()
    if existing and "data" in existing:
        for ch in existing["data"]:
            existing_names.add(ch["name"])

    channels: list[str] = []

    # Agent K webhook channel (receiver configs follow Alertmanager schema)
    if "agent-k-webhook" not in existing_names:
        result = api(
            "POST",
            "/channels",
            {
                "name": "agent-k-webhook",
                "type": "webhook",
                "webhook_configs": [
                    {
                        "url": AGENT_WEBHOOK_URL,
                        "send_resolved": True,
                    }
                ],
            },
        )
        if result:
            print("  ✅ Created agent-k-webhook channel")
            channels.append("agent-k-webhook")
        else:
            print("  ⚠️  Failed to create agent-k-webhook channel")
    else:
        print("  ✔️  agent-k-webhook channel already exists")
        channels.append("agent-k-webhook")

    # Slack channel (if configured)
    if SLACK_WEBHOOK_URL and "agent-k-slack" not in existing_names:
        result = api(
            "POST",
            "/channels",
            {
                "name": "agent-k-slack",
                "type": "slack",
                "slack_configs": [
                    {
                        "api_url": SLACK_WEBHOOK_URL,
                        "send_resolved": True,
                        "channel": "#agent-k",
                    }
                ],
            },
        )
        if result:
            print("  ✅ Created agent-k-slack channel")
            channels.append("agent-k-slack")
        else:
            print("  ⚠️  Failed to create agent-k-slack channel")
    elif SLACK_WEBHOOK_URL:
        print("  ✔️  agent-k-slack channel already exists")
        channels.append("agent-k-slack")

    return channels


def provision_alerts(channel_names: list[str]) -> None:
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

        # Inject notification channels (referenced by name); the rule API folds
        # these into condition.thresholds receivers.
        if "preferredChannels" not in alert_def:
            alert_def["preferredChannels"] = []
        for ch_name in channel_names:
            if ch_name not in alert_def["preferredChannels"]:
                alert_def["preferredChannels"].append(ch_name)

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
            stored = dash.get("data") or {}
            title = stored.get("title", "")
            if title:
                existing_titles.add(title)

    for dash_file in sorted(dashboards_dir.glob("*.json")):
        with open(dash_file) as f:
            dash_def = json.load(f)

        # Dashboard files may wrap the content in {"data": {...}}; the API
        # expects the dashboard content itself (title, widgets, ...).
        content = dash_def.get("data", dash_def)
        title = content.get("title", dash_file.stem)

        if title in existing_titles:
            print(f"  ✔️  Dashboard '{title}' already exists")
        else:
            result = api("POST", "/dashboards", content)
            if result:
                print(f"  ✅ Created dashboard '{title}'")
            else:
                print(f"  ⚠️  Failed to create dashboard '{title}'")


def _alert_names_from_files() -> set[str]:
    alerts_dir = PROVISIONING_DIR / "alerts"
    names: set[str] = set()
    for alert_file in alerts_dir.glob("*.json"):
        with open(alert_file) as f:
            names.add(json.load(f).get("alert", alert_file.stem))
    return names


def _dashboard_titles_from_files() -> set[str]:
    dashboards_dir = PROVISIONING_DIR / "dashboards"
    titles: set[str] = set()
    for dash_file in dashboards_dir.glob("*.json"):
        with open(dash_file) as f:
            dash_def = json.load(f)
            content = dash_def.get("data", dash_def)
            titles.add(content.get("title", dash_file.stem))
    return titles


def _view_names_from_files() -> set[str]:
    views_dir = PROVISIONING_DIR / "views"
    names: set[str] = set()
    for view_file in views_dir.glob("*.json"):
        with open(view_file) as f:
            names.add(json.load(f).get("name", view_file.stem))
    return names


def delete_agent_k_resources() -> None:
    """Remove Agent K dashboards, alerts, and views so provision can recreate them."""
    print("🗑️  Deleting existing Agent K SigNoz resources...\n")

    target_titles = _dashboard_titles_from_files()
    existing = api("GET", "/dashboards")
    if existing and isinstance(existing.get("data"), list):
        for dash in existing["data"]:
            stored = dash.get("data") or {}
            title = stored.get("title", "")
            if title in target_titles:
                dash_id = dash.get("id")
                if dash_id:
                    resp = httpx.delete(
                        f"{SIGNOZ_URL}/api/v1/dashboards/{dash_id}",
                        headers=HEADERS,
                        timeout=30.0,
                    )
                    if resp.status_code in (200, 204):
                        print(f"  🗑️  Deleted dashboard '{title}'")
                    else:
                        print(
                            f"  ⚠️  Failed to delete dashboard '{title}': "
                            f"{resp.status_code}"
                        )

    target_alerts = _alert_names_from_files()
    existing = api("GET", "/rules")
    rules = (existing.get("data") or {}).get("rules", []) if existing else []
    for rule in rules:
        alert_name = rule.get("alert", "")
        if alert_name in target_alerts:
            rule_id = rule.get("id")
            if rule_id:
                resp = httpx.delete(
                    f"{SIGNOZ_URL}/api/v1/rules/{rule_id}",
                    headers=HEADERS,
                    timeout=30.0,
                )
                if resp.status_code == 200:
                    print(f"  🗑️  Deleted alert '{alert_name}'")
                else:
                    print(
                        f"  ⚠️  Failed to delete alert '{alert_name}': "
                        f"{resp.status_code}"
                    )

    target_views = _view_names_from_files()
    existing = api("GET", "/explorer/views")
    view_rows = existing.get("data") if existing else None
    if isinstance(view_rows, list):
        for view in view_rows:
            name = view.get("name", "")
            tags = view.get("tags") or []
            if name in target_views or "agent-k" in tags:
                view_id = view.get("id")
                if view_id:
                    resp = httpx.delete(
                        f"{SIGNOZ_URL}/api/v1/explorer/views/{view_id}",
                        headers=HEADERS,
                        timeout=30.0,
                    )
                    if resp.status_code == 200:
                        print(f"  🗑️  Deleted view '{name}'")
                    else:
                        print(
                            f"  ⚠️  Failed to delete view '{name}': {resp.status_code}"
                        )
    print()


def provision_fresh() -> None:
    """Delete and recreate Agent K dashboards, alerts, and views."""
    delete_agent_k_resources()
    channel_names = provision_notification_channels()
    provision_alerts(channel_names)
    provision_dashboards()
    provision_views()


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

    channel_names = provision_notification_channels()

    provision_alerts(channel_names)
    provision_dashboards()
    provision_views()

    print("\n✅ Provisioning complete!")


def provision_views() -> None:
    """Provision saved Explorer views from JSON files."""
    print("\n🔭 Provisioning saved Explorer views...")

    views_dir = PROVISIONING_DIR / "views"
    if not views_dir.exists():
        print("  ⚠️  No views directory found")
        return

    existing = api("GET", "/explorer/views")
    existing_names: set[str] = set()
    if existing and isinstance(existing.get("data"), list):
        for view in existing["data"]:
            if view.get("name"):
                existing_names.add(view["name"])

    for view_file in sorted(views_dir.glob("*.json")):
        with open(view_file) as f:
            view_def = json.load(f)

        name = view_def.get("name", view_file.stem)
        cq = view_def.get("compositeQuery") or {}
        if cq.get("queryType") == "builder" and "panelType" not in cq:
            cq = {**cq, "panelType": "list"}
            view_def = {**view_def, "compositeQuery": cq}
        if name in existing_names:
            print(f"  ✔️  View '{name}' already exists")
            continue

        result = api("POST", "/explorer/views", view_def)
        if result:
            print(f"  ✅ Created view '{name}'")
        else:
            print(f"  ⚠️  Failed to create view '{name}'")


if __name__ == "__main__":
    import sys

    if "--fresh" in sys.argv:
        print("🕶️  Agent K — Fresh provisioning\n")
        print(f"SigNoz URL: {SIGNOZ_URL}")
        provision_fresh()
        print("\n✅ Fresh provisioning complete!")
    else:
        main()

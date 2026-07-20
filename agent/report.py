"""Report generation — markdown templates and SigNoz link rewriting."""

from __future__ import annotations

import re
from typing import Any

import markdown as md

from config import settings


# ── Link helpers ──────────────────────────────────────────────────


def link_trace(trace_id: str) -> str:
    """Generate a SigNoz trace deep link."""
    return f"{settings.signoz_url}/trace/{trace_id}"


def link_explorer(
    data_source: str = "traces",
    filters: dict[str, str] | None = None,
) -> str:
    """Generate a SigNoz explorer deep link."""
    base = f"{settings.signoz_url}/traces-explorer"
    if data_source == "logs":
        base = f"{settings.signoz_url}/logs-explorer"
    elif data_source == "metrics":
        base = f"{settings.signoz_url}/metrics-explorer"

    if filters:
        params = "&".join(f"{k}={v}" for k, v in filters.items())
        return f"{base}?{params}"
    return base


# ── signoz:// placeholder rewriting ──────────────────────────────


def rewrite_signoz_links(report_md: str) -> str:
    """Rewrite signoz:// placeholder URLs to real SigNoz URLs.

    Handles:
      signoz://trace/<trace_id> -> {SIGNOZ_URL}/trace/<trace_id>
      signoz://explorer/<params> -> {SIGNOZ_URL}/traces-explorer?<params>
      signoz://logs/<params> -> {SIGNOZ_URL}/logs-explorer?<params>
      signoz://metrics/<params> -> {SIGNOZ_URL}/metrics-explorer?<params>
    """
    base = settings.signoz_url.rstrip("/")

    # Trace links
    report_md = re.sub(
        r"signoz://trace/([a-fA-F0-9]+)",
        rf"{base}/trace/\1",
        report_md,
    )

    # Explorer links
    report_md = re.sub(
        r"signoz://explorer/?(.*?)\)",
        rf"{base}/traces-explorer?\1)",
        report_md,
    )

    # Logs links
    report_md = re.sub(
        r"signoz://logs/?(.*?)\)",
        rf"{base}/logs-explorer?\1)",
        report_md,
    )

    # Metrics links
    report_md = re.sub(
        r"signoz://metrics/?(.*?)\)",
        rf"{base}/metrics-explorer?\1)",
        report_md,
    )

    # Catch-all: any remaining signoz:// URLs
    report_md = report_md.replace("signoz://", f"{base}/")

    return report_md


# ── Report rendering ─────────────────────────────────────────────


def render_html(report_md: str) -> str:
    """Render markdown report to HTML string."""
    extensions = ["tables", "fenced_code", "codehilite", "toc", "nl2br"]
    html_body = md.markdown(report_md, extensions=extensions)
    return html_body


def format_investigation_for_list(inv: dict[str, Any]) -> dict[str, Any]:
    """Format an investigation record for the reports list page."""
    return {
        "id": inv.get("id", ""),
        "alertname": _extract_alertname(inv.get("trigger_json", "")),
        "status": inv.get("status", "unknown"),
        "started_at": inv.get("started_at", ""),
        "finished_at": inv.get("finished_at", ""),
        "root_cause": inv.get("root_cause", ""),
        "cost_usd": inv.get("cost_usd", 0.0),
        "tokens_in": inv.get("tokens_in", 0),
        "tokens_out": inv.get("tokens_out", 0),
        "trace_id": inv.get("trace_id", ""),
    }


def _extract_alertname(trigger_json: str) -> str:
    """Extract alertname from trigger JSON."""
    try:
        import json
        data = json.loads(trigger_json)
        return data.get("alertname", "unknown")
    except (json.JSONDecodeError, TypeError):
        return "unknown"

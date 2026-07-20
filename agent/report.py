"""Report generation — markdown templates and SigNoz link rewriting."""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from typing import Any

import markdown as md

from config import settings


# ── Link helpers ──────────────────────────────────────────────────


def link_trace(trace_id: str) -> str:
    """Generate a SigNoz trace deep link."""
    return f"{settings.signoz_url.rstrip('/')}/trace/{trace_id}"


def link_explorer(
    signal: str = "traces",
    filter_expression: str = "",
    minutes: int = 60,
) -> str:
    """Generate a SigNoz explorer deep link with a working filter.

    The explorer reads its state from a `compositeQuery` URL param carrying a
    (double-URL-encoded) builder query whose filter is a v5 expression string.
    """
    base = settings.signoz_url.rstrip("/")
    route = {
        "traces": "/traces-explorer",
        "logs": "/logs-explorer",
        "metrics": "/metrics-explorer",
    }.get(signal, "/traces-explorer")

    query_data: dict[str, Any] = {
        "queryName": "A",
        "dataSource": signal if signal in ("traces", "logs", "metrics") else "traces",
        "aggregateOperator": "noop",
        "aggregateAttribute": {"key": ""},
        "expression": "A",
        "disabled": False,
    }
    if filter_expression:
        query_data["filter"] = {"expression": filter_expression}

    composite = {
        "queryType": "builder",
        "builder": {"queryData": [query_data], "queryFormulas": []},
    }
    # The UI expects the JSON percent-encoded twice (%2522 for a quote).
    encoded = urllib.parse.quote(
        urllib.parse.quote(json.dumps(composite), safe=""), safe=""
    )

    end_ns = int(time.time() * 1_000_000_000)
    start_ns = end_ns - minutes * 60 * 1_000_000_000
    return (
        f"{base}{route}?compositeQuery={encoded}&startTime={start_ns}&endTime={end_ns}"
    )


# ── signoz:// placeholder rewriting ──────────────────────────────

_EXPLORER_RE = re.compile(r"signoz://explorer/(traces|logs|metrics)\?([^)\n]*)")


def rewrite_signoz_links(report_md: str) -> str:
    """Rewrite signoz:// placeholder URLs to real SigNoz URLs.

    Grammar the LLM is instructed to use (see playbook):
      signoz://trace/<trace_id>
      signoz://explorer/<traces|logs|metrics>?<filter expression>

    The filter expression is plain v5 filter syntax (spaces and quotes fine —
    it gets URL-encoded here, so the final markdown link is valid).
    """
    base = settings.signoz_url.rstrip("/")

    # Trace links
    report_md = re.sub(
        r"signoz://trace/([a-fA-F0-9]+)",
        rf"{base}/trace/\1",
        report_md,
    )

    # Explorer links with filter expressions
    def _explorer_sub(m: re.Match[str]) -> str:
        signal = m.group(1)
        expr = m.group(2).strip()
        return link_explorer(signal, expr)

    report_md = _EXPLORER_RE.sub(_explorer_sub, report_md)

    # Bare explorer links without a filter
    report_md = re.sub(
        r"signoz://explorer/(traces|logs|metrics)",
        lambda m: link_explorer(m.group(1)),
        report_md,
    )

    # Catch-all: any remaining signoz:// URLs land on the SigNoz home page
    # rather than a broken scheme.
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

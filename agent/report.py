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


# Known saved view names → explorer routes (provisioned by make provision)
_VIEW_ROUTES: dict[str, str] = {
    "AstroMart — Failing checkouts": "/traces-explorer",
    "AstroMart — Slow checkout": "/traces-explorer",
    "AstroMart — Secret leak logs": "/logs-explorer",
    "AstroMart — Deploy markers": "/logs-explorer",
    "Agent K — LLM calls": "/traces-explorer",
    "Agent K — MCP tool calls": "/traces-explorer",
}


def link_view(view_name: str) -> str:
    """Link to a provisioned saved view in SigNoz (opens explorer tab)."""
    base = settings.signoz_url.rstrip("/")
    route = _VIEW_ROUTES.get(view_name, "/traces-explorer")
    encoded = urllib.parse.quote(view_name, safe="")
    return f"{base}{route}?viewName={encoded}"


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

    # Saved view shortcuts
    report_md = re.sub(
        r"signoz://view/([^)\n]+)",
        lambda m: link_view(m.group(1).strip()),
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


# ── Auditor badge ────────────────────────────────────────────────


def audit_badge(audit: Any) -> str:
    """Build a one-line groundedness badge from an AuditResult for the report header."""
    outcome = getattr(audit, "outcome", "skipped")
    if outcome == "skipped":
        return ""
    if outcome == "grounded":
        score = getattr(audit, "score", None)
        pct = f" ({score * 100:.0f}% of claims backed)" if score is not None else ""
        return f"> ✅ **Independent audit: grounded**{pct} — every factual claim is backed by collected evidence.\n\n"
    if outcome == "ungrounded":
        claims = getattr(audit, "unsupported_claims", []) or []
        n = len(claims)
        head = (
            f"> ⚠️ **Independent audit: UNGROUNDED** — {n} unsupported "
            f"claim{'s' if n != 1 else ''} flagged by a second, independent model:\n"
        )
        bullets = "".join(f">   - {c}\n" for c in claims[:5])
        notes = getattr(audit, "notes", "")
        tail = f">\n> _{notes}_\n\n" if notes else "\n"
        return head + bullets + tail
    # error
    err = getattr(audit, "error", "")
    return (
        f"> ⚠️ **Independent audit: could not run** — this RCA was published "
        f"WITHOUT a groundedness check ({err[:160]}).\n\n"
    )


def prepend_audit_badge(report_md: str, audit: Any) -> str:
    """Insert the audit badge just under the report's title line."""
    badge = audit_badge(audit)
    if not badge:
        return report_md
    lines = report_md.split("\n", 1)
    if lines and lines[0].startswith("#"):
        rest = lines[1] if len(lines) > 1 else ""
        return f"{lines[0]}\n\n{badge}{rest}"
    return badge + report_md


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
        "audit_grounded": inv.get("audit_grounded"),
        "audit_score": inv.get("audit_score"),
    }


def _extract_alertname(trigger_json: str) -> str:
    """Extract alertname from trigger JSON."""
    try:
        import json

        data = json.loads(trigger_json)
        return data.get("alertname", "unknown")
    except (json.JSONDecodeError, TypeError):
        return "unknown"

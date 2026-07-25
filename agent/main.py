"""Agent K — FastAPI application: webhooks, investigation triggers, approvals, reports."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import markdown
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from config import settings
from models import AlertmanagerWebhook, InvestigationTrigger, InvestigateRequest
from store import store
from telemetry import setup_telemetry
from loop import run_investigation
from remediation import execute_action, verify_action
from slack import post_verification

logger = logging.getLogger("agent-k")

app = FastAPI(title="Agent K", version="1.0.0", description="AI SRE Sidekick")
setup_telemetry(app)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Track running investigations to dedupe
_running_alerts: set[str] = set()
# Keep refs to fire-and-forget tasks so they aren't garbage-collected mid-flight
_background_tasks: set[asyncio.Task] = set()


def should_investigate(
    alertname: str, alert_status: str, fingerprint: str = ""
) -> tuple[bool, str]:
    """Decide whether an incoming alert warrants a new investigation.

    Alerts re-fire on every evaluation interval, and SigNoz also notifies on
    resolution — without this gate the agent would re-investigate the same
    incident every minute and burn tokens on alerts that already recovered.
    """
    if alert_status == "resolved":
        return False, "alert is resolved"

    if fingerprint.startswith("test-"):
        return True, "test webhook (cooldown bypass)"

    if alertname in _running_alerts:
        return False, "investigation already running"

    latest = store.latest_investigation_for_alert(alertname)
    # A failed run must not suppress retrying a still-firing alert — the
    # cooldown exists to stop re-investigating *answered* incidents, not to
    # silence the agent after its own crash.
    if latest and latest.get("status") == "failed":
        return True, "retrying after failed investigation"
    if latest and latest.get("started_at"):
        try:
            started = datetime.fromisoformat(latest["started_at"])
            age_min = (datetime.now(timezone.utc) - started).total_seconds() / 60
            if age_min < settings.investigation_cooldown_minutes:
                return False, (
                    f"cooldown — investigated {age_min:.0f} min ago "
                    f"(window: {settings.investigation_cooldown_minutes} min)"
                )
        except ValueError:
            pass

    return True, "new incident"


# ── Webhook endpoint ────────────────────────────────────────────


@app.post("/webhook/signoz")
async def webhook_signoz(
    request: Request, background_tasks: BackgroundTasks
) -> JSONResponse:
    """Receive SigNoz/Alertmanager-style alert webhook."""
    body = await request.json()
    logger.info("Received webhook: %s", json.dumps(body)[:500])

    try:
        webhook = AlertmanagerWebhook.model_validate(body)
    except Exception as exc:
        logger.error("Failed to parse webhook payload: %s", exc)
        return JSONResponse(
            status_code=400, content={"error": f"Invalid payload: {exc}"}
        )

    accepted: list[str] = []
    for alert in webhook.alerts:
        alertname = alert.labels.get("alertname", "unknown")

        ok, reason = should_investigate(
            alertname, alert.status.value, alert.fingerprint
        )
        if not ok:
            logger.info("Skipping alert %s: %s", alertname, reason)
            continue

        trigger = InvestigationTrigger.from_webhook(AlertmanagerWebhook(alerts=[alert]))

        _running_alerts.add(alertname)
        background_tasks.add_task(_investigate_and_cleanup, trigger, alertname)
        accepted.append(alertname)

    return JSONResponse(
        status_code=200, content={"status": "accepted", "investigating": accepted}
    )


async def _investigate_and_cleanup(
    trigger: InvestigationTrigger, alertname: str
) -> None:
    """Run investigation and clean up tracking set."""
    try:
        investigation_id = store.create_investigation(
            trigger_json=trigger.model_dump_json()
        )
        await run_investigation(trigger, investigation_id)
    finally:
        _running_alerts.discard(alertname)


# ── Manual trigger ──────────────────────────────────────────────


@app.post("/investigate")
async def investigate(
    body: InvestigateRequest, background_tasks: BackgroundTasks
) -> JSONResponse:
    """Manually trigger an investigation."""
    trigger = InvestigationTrigger.from_manual(body.prompt)

    investigation_id = store.create_investigation(
        trigger_json=trigger.model_dump_json()
    )
    background_tasks.add_task(run_investigation, trigger, investigation_id)
    return JSONResponse(
        status_code=202,
        content={"status": "investigation started", "prompt": body.prompt},
    )


# ── Approval endpoint ──────────────────────────────────────────


def _verify_hmac(action_id: str, sig: str) -> bool:
    """Verify HMAC signature for approval links."""
    expected = hmac.new(
        settings.approval_secret.encode(),
        action_id.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, sig)


@app.get("/approve/{action_id}")
async def approve_action(action_id: str, sig: str = Query(...)) -> HTMLResponse:
    """Approve and execute a remediation action."""
    if not _verify_hmac(action_id, sig):
        raise HTTPException(status_code=403, detail="Invalid signature")

    action = store.get_action(action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="Action not found")

    if action["status"] != "proposed":
        return HTMLResponse(
            content=f"""
            <html><body style="background:#1a1a2e;color:#e0e0e0;font-family:monospace;padding:2rem">
            <h1>🕶️ Agent K</h1>
            <p>Action <code>{action_id}</code> is already <strong>{action["status"]}</strong>.</p>
            </body></html>
            """
        )

    # Execute the action
    store.update_action(action_id, status="approved")

    params = json.loads(action["params_json"]) if action["params_json"] else {}
    result = await execute_action(action["kind"], params)

    store.update_action(
        action_id, status="executed", executed_at=datetime.now(timezone.utc).isoformat()
    )

    # Start verification in background (keep a ref so the task isn't GC'd)
    task = asyncio.create_task(_verify_and_update(action_id, action, params, result))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return HTMLResponse(
        content=f"""
        <html><body style="background:#1a1a2e;color:#e0e0e0;font-family:monospace;padding:2rem">
        <h1>🕶️ Agent K — Action Approved</h1>
        <p>✅ Action <code>{action["kind"]}</code> executed successfully.</p>
        <pre>{result}</pre>
        <p>Verification in progress...</p>
        </body></html>
        """
    )


async def _verify_and_update(
    action_id: str, action: dict[str, Any], params: dict[str, Any], exec_result: str
) -> None:
    """Run verification and update action + Slack."""
    verification = await verify_action(action["kind"], params)
    store.update_action(action_id, status="verified", verification_md=verification)

    # Post verification to Slack
    investigation = store.get_investigation(action["investigation_id"])
    if investigation:
        await post_verification(
            investigation_id=action["investigation_id"],
            action_id=action_id,
            kind=action["kind"],
            service=params.get("service", ""),
            result=verification,
            success="✅" in verification,
        )


# ── Reports ─────────────────────────────────────────────────────


def _serialize_investigation(inv: dict) -> dict:
    """Public JSON shape for an investigation."""
    from report import format_investigation_for_list

    base = format_investigation_for_list(inv)
    base["report_md"] = inv.get("report_md")
    base["finished_at"] = inv.get("finished_at")
    return base


@app.get("/api/investigations")
async def api_list_investigations(limit: int = 50) -> JSONResponse:
    """List investigations as JSON (for test harness and automation)."""
    investigations = store.list_investigations(limit=limit)
    return JSONResponse(
        content={"investigations": [_serialize_investigation(i) for i in investigations]}
    )


@app.get("/api/investigations/{investigation_id}")
async def api_get_investigation(investigation_id: str) -> JSONResponse:
    """Get one investigation including report markdown."""
    investigation = store.get_investigation(investigation_id)
    if investigation is None:
        raise HTTPException(status_code=404, detail="Investigation not found")
    return JSONResponse(content=_serialize_investigation(investigation))


@app.get("/reports", response_class=HTMLResponse)
async def list_reports(request: Request) -> HTMLResponse:
    """List all investigation reports."""
    investigations = store.list_investigations()
    return templates.TemplateResponse(
        "reports.html",
        {"request": request, "investigations": investigations},
    )


@app.get("/reports/{investigation_id}", response_class=HTMLResponse)
async def get_report(investigation_id: str, request: Request) -> HTMLResponse:
    """View a single investigation report."""
    investigation = store.get_investigation(investigation_id)
    if investigation is None:
        raise HTTPException(status_code=404, detail="Investigation not found")

    report_html = ""
    if investigation.get("report_md"):
        report_html = markdown.markdown(
            investigation["report_md"],
            extensions=["fenced_code", "tables", "nl2br"],
        )

    return templates.TemplateResponse(
        "report_detail.html",
        {
            "request": request,
            "investigation": investigation,
            "report_html": report_html,
            "signoz_url": settings.signoz_url,
        },
    )


# ── Health ──────────────────────────────────────────────────────


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/reports")


@app.get("/reports/{investigation_id}/transcript")
async def get_transcript(investigation_id: str) -> JSONResponse:
    """Full LLM/tool transcript for an investigation — the audit trail."""
    investigation = store.get_investigation(investigation_id)
    if investigation is None:
        raise HTTPException(status_code=404, detail="Investigation not found")
    transcript = json.loads(investigation.get("transcript_json") or "[]")
    return JSONResponse(content={"id": investigation_id, "transcript": transcript})


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "agent-k"}

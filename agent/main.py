"""Agent K — FastAPI application: webhooks, investigation triggers, approvals, reports."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from pathlib import Path
from typing import Any

import markdown
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from config import Settings
from models import AlertmanagerWebhook, InvestigationTrigger, InvestigateRequest
from store import Store
from telemetry import setup_telemetry
from loop import run_investigation
from remediation import execute_action, verify_action
from slack import post_verification

logger = logging.getLogger("agent-k")

settings = Settings()
app = FastAPI(title="Agent K", version="1.0.0", description="AI SRE Sidekick")
tracer = setup_telemetry(app)
store = Store(settings.db_path)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Track running investigations to dedupe
_running_alerts: set[str] = set()


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

    for alert in webhook.alerts:
        alertname = alert.labels.get("alertname", "unknown")

        # Dedupe: skip if already investigating this alert
        if alertname in _running_alerts:
            logger.info("Skipping duplicate alert: %s (already investigating)", alertname)
            continue

        trigger = InvestigationTrigger.from_webhook(
            AlertmanagerWebhook(alerts=[alert])
        )

        _running_alerts.add(alertname)
        background_tasks.add_task(_investigate_and_cleanup, trigger, alertname)

    return JSONResponse(status_code=200, content={"status": "accepted"})


async def _investigate_and_cleanup(
    trigger: InvestigationTrigger, alertname: str
) -> None:
    """Run investigation and clean up tracking set."""
    try:
        await run_investigation(trigger, settings, store)
    finally:
        _running_alerts.discard(alertname)


# ── Manual trigger ──────────────────────────────────────────────


@app.post("/investigate")
async def investigate(
    body: InvestigateRequest, background_tasks: BackgroundTasks
) -> JSONResponse:
    """Manually trigger an investigation."""
    trigger = InvestigationTrigger.from_manual(body.prompt)

    background_tasks.add_task(run_investigation, trigger, settings, store)
    return JSONResponse(
        status_code=202, content={"status": "investigation started", "prompt": body.prompt}
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
async def approve_action(
    action_id: str, sig: str = Query(...), request: Request = None
) -> HTMLResponse:
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
            <p>Action <code>{action_id}</code> is already <strong>{action['status']}</strong>.</p>
            </body></html>
            """
        )

    # Execute the action
    store.update_action(action_id, status="approved")

    params = json.loads(action["params_json"]) if action["params_json"] else {}
    result = await execute_action(action["kind"], params)

    store.update_action(action_id, status="executed", executed_at="now")

    # Start verification in background
    asyncio.create_task(_verify_and_update(action_id, action, params, result))

    return HTMLResponse(
        content=f"""
        <html><body style="background:#1a1a2e;color:#e0e0e0;font-family:monospace;padding:2rem">
        <h1>🕶️ Agent K — Action Approved</h1>
        <p>✅ Action <code>{action['kind']}</code> executed successfully.</p>
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
            action_kind=action["kind"],
            exec_result=exec_result,
            verification_result=verification,
            settings=settings,
        )


# ── Reports ─────────────────────────────────────────────────────


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
        },
    )


# ── Health ──────────────────────────────────────────────────────


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "agent-k"}

"""Slack integration — post RCA reports and remediation proposals via incoming webhook."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

import httpx

from config import settings

logger = logging.getLogger(__name__)


# ── HMAC approval link generation ─────────────────────────────────


def generate_approval_signature(action_id: str) -> str:
    """Generate HMAC-SHA256 signature for an approval link."""
    return hmac.new(
        settings.approval_secret.encode("utf-8"),
        action_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_approval_signature(action_id: str, signature: str) -> bool:
    """Verify HMAC-SHA256 signature for an approval link."""
    expected = generate_approval_signature(action_id)
    return hmac.compare_digest(expected, signature)


def build_approval_url(action_id: str) -> str:
    """Build the full approval URL with HMAC signature."""
    sig = generate_approval_signature(action_id)
    return f"{settings.agent_public_url}/approve/{action_id}?sig={sig}"


# ── Slack messaging ───────────────────────────────────────────────


async def post_rca(
    investigation: dict[str, Any],
    report_md: str,
) -> None:
    """Post RCA report to Slack via incoming webhook using Block Kit."""
    if not settings.slack_webhook_url:
        logger.info("No SLACK_WEBHOOK_URL configured — skipping Slack notification")
        return

    inv_id = investigation.get("id", "unknown")
    root_cause = investigation.get("root_cause", "Unknown root cause")
    cost_usd = investigation.get("cost_usd", 0.0)
    tokens_in = investigation.get("tokens_in", 0)
    tokens_out = investigation.get("tokens_out", 0)
    trace_id = investigation.get("trace_id", "")
    status = investigation.get("status", "done")

    # Extract alertname from trigger
    alertname = "unknown"
    try:
        trigger = json.loads(investigation.get("trigger_json", "{}"))
        alertname = trigger.get("alertname", "unknown")
    except (json.JSONDecodeError, TypeError):
        pass

    # Truncate report for Slack (3000 char limit per section)
    report_preview = report_md[:2800] + "\n..." if len(report_md) > 2800 else report_md

    # Build Block Kit message
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🕶️ Agent K — {root_cause}",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*Alert:*\n{alertname}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Status:*\n{status}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Investigation:*\n{inv_id}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Cost:*\n${cost_usd:.4f} ({tokens_in + tokens_out} tokens)",
                },
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": report_preview,
            },
        },
    ]

    # Add trace link if available
    if trace_id:
        trace_url = f"{settings.signoz_url}/trace/{trace_id}"
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"📊 <{trace_url}|View Agent K's investigation trace in SigNoz>",
            },
        })

    # Add report page link
    report_url = f"{settings.agent_public_url}/reports/{inv_id}"
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"📋 <{report_url}|View full report>",
        },
    })

    payload = {"blocks": blocks}
    await _send_webhook(payload)
    logger.info("Posted RCA to Slack for investigation %s", inv_id)


async def post_remediation_proposal(
    investigation_id: str,
    action_id: str,
    kind: str,
    service: str,
    reason: str,
) -> None:
    """Post a remediation proposal with approval button to Slack."""
    if not settings.slack_webhook_url:
        logger.info("No SLACK_WEBHOOK_URL — skipping remediation notification")
        return

    approval_url = build_approval_url(action_id)

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🔧 Agent K — Remediation Proposed",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*Investigation:*\n{investigation_id}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Action:*\n{kind}({service})",
                },
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Reason:* {reason}",
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"✅ <{approval_url}|Approve and execute remediation>",
            },
        },
    ]

    # If auto-approve is on, note it
    if settings.auto_approve:
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "⚡ AUTO_APPROVE is enabled — remediation will execute immediately.",
                },
            ],
        })

    payload = {"blocks": blocks}
    await _send_webhook(payload)
    logger.info("Posted remediation proposal to Slack: %s(%s)", kind, service)


async def post_verification(
    investigation_id: str,
    action_id: str,
    kind: str,
    service: str,
    result: str,
    success: bool,
) -> None:
    """Post remediation verification result to Slack."""
    if not settings.slack_webhook_url:
        return

    emoji = "✅" if success else "❌"
    status_text = "Verified — recovery confirmed" if success else "Verification inconclusive"

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{emoji} Agent K — Remediation {status_text}",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*Investigation:*\n{investigation_id}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Action:*\n{kind}({service})",
                },
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Result:*\n{result[:2000]}",
            },
        },
    ]

    payload = {"blocks": blocks}
    await _send_webhook(payload)


# ── Internal ──────────────────────────────────────────────────────


async def _send_webhook(payload: dict[str, Any]) -> None:
    """Send a payload to the configured Slack incoming webhook."""
    if not settings.slack_webhook_url:
        return

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            settings.slack_webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            logger.error(
                "Slack webhook returned %d: %s",
                resp.status_code,
                resp.text[:200],
            )

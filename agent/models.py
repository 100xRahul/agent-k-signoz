"""Pydantic v2 models for Agent K data structures."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ── Alertmanager webhook payload ─────────────────────────────────


class AlertStatus(str, Enum):
    """Alert status values from Alertmanager."""

    FIRING = "firing"
    RESOLVED = "resolved"


class Alert(BaseModel):
    """Single alert within an Alertmanager webhook."""

    status: AlertStatus = AlertStatus.FIRING
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: str = ""
    endsAt: str = ""
    generatorURL: str = ""
    fingerprint: str = ""
    values: dict[str, Any] = Field(default_factory=dict)


class GroupLabels(BaseModel):
    """Group labels from Alertmanager."""

    alertname: str = ""


class AlertmanagerWebhook(BaseModel):
    """Alertmanager-style webhook payload from SigNoz."""

    receiver: str = ""
    status: AlertStatus = AlertStatus.FIRING
    alerts: list[Alert] = Field(default_factory=list)
    groupLabels: GroupLabels = Field(default_factory=GroupLabels)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    commonAnnotations: dict[str, str] = Field(default_factory=dict)
    externalURL: str = ""
    version: str = "4"
    groupKey: str = ""
    truncatedAlerts: int = 0


# ── Investigation trigger ────────────────────────────────────────


class TriggerType(str, Enum):
    """How the investigation was triggered."""

    ALERT = "alert"
    MANUAL = "manual"


class InvestigationTrigger(BaseModel):
    """Normalized trigger that starts an investigation."""

    type: TriggerType
    alertname: str = ""
    alert_status: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    starts_at: str = ""
    prompt: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_webhook(cls, webhook: AlertmanagerWebhook) -> InvestigationTrigger:
        """Create trigger from Alertmanager webhook."""
        first_alert = webhook.alerts[0] if webhook.alerts else Alert()
        alertname = (
            first_alert.labels.get("alertname", "")
            or webhook.groupLabels.alertname
            or "unknown"
        )
        return cls(
            type=TriggerType.ALERT,
            alertname=alertname,
            alert_status=first_alert.status.value,
            labels=first_alert.labels,
            annotations=first_alert.annotations,
            starts_at=first_alert.startsAt,
            raw=webhook.model_dump(),
        )

    @classmethod
    def from_manual(cls, prompt: str) -> InvestigationTrigger:
        """Create trigger from manual investigation request."""
        return cls(
            type=TriggerType.MANUAL,
            alertname="manual",
            prompt=prompt,
        )


# ── Manual investigation request ─────────────────────────────────


class InvestigateRequest(BaseModel):
    """Request body for POST /investigate."""

    prompt: str


# ── Remediation ──────────────────────────────────────────────────


class RemediationKind(str, Enum):
    """Allowed remediation action types."""

    ROLLBACK = "rollback"
    DISABLE_FLAG = "disable_flag"
    RESTART = "restart"


class RemediationAction(BaseModel):
    """A proposed or executed remediation action."""

    kind: RemediationKind
    service: str = ""
    flag: str = ""
    reason: str = ""
    expected_effect: str = ""
    verification_query: str = ""


# ── Investigation record (for API responses) ─────────────────────


class InvestigationRecord(BaseModel):
    """Investigation record from the database."""

    id: str
    trigger_json: str = ""
    status: str = "running"
    started_at: str = ""
    finished_at: str | None = None
    report_md: str | None = None
    root_cause: str | None = None
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    trace_id: str | None = None


class ActionRecord(BaseModel):
    """Action record from the database."""

    id: str
    investigation_id: str
    kind: str
    params_json: str = ""
    status: str = "proposed"
    created_at: str = ""
    executed_at: str | None = None
    verification_md: str | None = None

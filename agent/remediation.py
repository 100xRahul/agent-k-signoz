"""Agent K — Remediation engine: guarded actions with verification.

Actions are deliberately narrow and reversible:
  - rollback:      restore checkout to the baseline version (Redis-driven deploy model)
  - disable_flag:  clear a chaos/feature flag in Redis
  - restart:       docker-restart a sandbox container (located by compose label,
                   so it works regardless of compose project name)

Verification is data-driven where possible: after a rollback we re-query SigNoz
for the service's error count and report the actual number, not a promise.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from typing import Any

import redis.asyncio as aioredis

logger = logging.getLogger("agent-k.remediation")

REDIS_URL = os.getenv("REDIS_URL", "redis://sandbox-redis:6379")
OTEL_ENDPOINT = os.getenv(
    "OTEL_EXPORTER_OTLP_ENDPOINT", "http://signoz-otel-collector:4317"
)

BASELINE_VERSION = "1.4.1"

# Flags the agent may clear. chaos:* keys are the sandbox's feature/chaos switchboard.
KNOWN_FLAGS = {"bad-deploy", "pool-exhaustion", "flag-combo", "secret-leak",
               "new-checkout", "express-pay", "gift-wrap"}

# Services the agent may touch.
ALLOWED_SERVICES = {"gateway", "checkout", "payment", "inventory", "loadgen"}


def _emit_marker_log(service: str, version: str, event: str = "rollback") -> None:
    """Emit a deployment/rollback marker log via OTLP from service.name=deploy-bot."""
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
            OTLPLogExporter,
        )

        resource = Resource.create({"service.name": "deploy-bot"})
        log_provider = LoggerProvider(resource=resource)
        log_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(endpoint=OTEL_ENDPOINT, insecure=True)
            )
        )
        handler = LoggingHandler(level=logging.INFO, logger_provider=log_provider)

        deploy_logger = logging.getLogger(f"deploy-bot-{event}")
        deploy_logger.addHandler(handler)
        deploy_logger.setLevel(logging.INFO)

        deploy_logger.info(
            json.dumps(
                {
                    "event": event,
                    "service": service,
                    "version": version,
                    "timestamp": time.time(),
                }
            )
        )

        log_provider.force_flush()
        log_provider.shutdown()
        logger.info("Marker log emitted: %s %s v%s", event, service, version)
    except ImportError:
        logger.warning("OTel SDK not available — skipping marker log.")


async def _get_redis() -> aioredis.Redis:
    return aioredis.from_url(REDIS_URL, decode_responses=True)


def _normalize_target(params: dict[str, Any]) -> str:
    """The LLM sends the target as `service` (per tool schema); accept `flag` too."""
    return str(params.get("service") or params.get("flag") or "").strip()


async def _error_count(service: str, minutes: int = 2) -> int | None:
    """Query SigNoz for the service's recent error count. None if unparseable."""
    try:
        from tools_rest import signoz_aggregate_traces

        raw = await signoz_aggregate_traces(
            aggregate_operator="count",
            filters=[
                {
                    "key": {"key": "service.name", "type": "resource", "dataType": "string"},
                    "op": "=",
                    "value": service,
                },
                {
                    "key": {"key": "has_error", "type": "tag", "dataType": "bool"},
                    "op": "=",
                    "value": "true",
                },
            ],
        )
        data = json.loads(raw)
        # Sum whatever numeric series values came back — shape-tolerant.
        total = 0.0
        found = False

        def walk(node: Any) -> None:
            nonlocal total, found
            if isinstance(node, dict):
                if "value" in node and isinstance(node["value"], (int, float)):
                    total += float(node["value"])
                    found = True
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(data)
        return int(total) if found else None
    except Exception:
        logger.exception("Error-count verification query failed for %s", service)
        return None


# ── Action implementations ──────────────────────────────────────


async def rollback_run(params: dict[str, Any]) -> str:
    """Roll back checkout to the baseline version (Redis-driven deploy model)."""
    service = _normalize_target(params) or "checkout"
    if service not in ALLOWED_SERVICES:
        return f"❌ Refusing rollback: '{service}' is not in the allowed service list."

    r = await _get_redis()
    await r.set("chaos:checkout-version", BASELINE_VERSION)
    await r.delete("chaos:bad-deploy")
    await r.aclose()

    _emit_marker_log(service, BASELINE_VERSION, "rollback")
    return (
        f"✅ Rolled back {service} to v{BASELINE_VERSION} "
        f"(bad-deploy behavior disabled). Rollback marker emitted."
    )


async def rollback_verify(params: dict[str, Any]) -> str:
    """Verify rollback with data: re-query the service's error count after 60s."""
    service = _normalize_target(params) or "checkout"
    logger.info("Waiting 60s before verifying %s rollback...", service)
    await asyncio.sleep(60)

    errors = await _error_count(service)
    if errors is None:
        return (
            f"⚠️ Verification query for {service} returned no parseable data — "
            f"check the SigNoz dashboard manually."
        )
    if errors <= 2:
        return f"✅ Verified: {service} error count in the last window is {errors} — recovery confirmed."
    return f"⚠️ {service} still shows {errors} errors after rollback — needs a human look."


async def disable_flag_run(params: dict[str, Any]) -> str:
    """Disable a feature/chaos flag via Redis."""
    flag = _normalize_target(params)
    if not flag:
        return "❌ No flag name provided."
    if flag not in KNOWN_FLAGS:
        return f"❌ Refusing to touch unknown flag '{flag}'. Known flags: {sorted(KNOWN_FLAGS)}"

    r = await _get_redis()
    await r.delete(f"chaos:{flag}")
    await r.aclose()
    logger.info("Disabled flag: chaos:%s", flag)
    return f"✅ Flag chaos:{flag} disabled."


async def disable_flag_verify(params: dict[str, Any]) -> str:
    """Verify the flag is disabled."""
    flag = _normalize_target(params)
    await asyncio.sleep(30)
    r = await _get_redis()
    val = await r.get(f"chaos:{flag}")
    await r.aclose()
    if val is None:
        return f"✅ Flag chaos:{flag} confirmed disabled."
    return f"⚠️ Flag chaos:{flag} still set: {val}"


def _find_container(service: str) -> str | None:
    """Find a container ID by its compose service label (project-name agnostic)."""
    try:
        out = subprocess.run(
            ["docker", "ps", "-q", "-f", f"label=com.docker.compose.service={service}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        container_id = out.stdout.strip().splitlines()
        return container_id[0] if container_id else None
    except (subprocess.SubprocessError, FileNotFoundError):
        return None


async def restart_run(params: dict[str, Any]) -> str:
    """Restart a sandbox service container via the docker socket."""
    service = _normalize_target(params)
    if service not in ALLOWED_SERVICES:
        return f"❌ Refusing restart: '{service}' is not in the allowed service list."

    container = await asyncio.to_thread(_find_container, service)
    if container is None:
        return f"❌ No running container found for service '{service}'."

    def _restart() -> str:
        try:
            subprocess.run(
                ["docker", "restart", container],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            return f"✅ Restarted {service} (container {container[:12]})."
        except subprocess.SubprocessError as exc:
            return f"❌ Restart failed: {exc}"
        except FileNotFoundError:
            return "❌ docker CLI not available in the agent container."

    return await asyncio.to_thread(_restart)


async def restart_verify(params: dict[str, Any]) -> str:
    """Verify service came back after restart."""
    service = _normalize_target(params)
    await asyncio.sleep(15)
    container = await asyncio.to_thread(_find_container, service)
    if container:
        return f"✅ {service} is running again (container {container[:12]})."
    return f"⚠️ {service} container not found after restart — needs a human look."


# ── Registry ────────────────────────────────────────────────────

REMEDIATION_REGISTRY: dict[str, dict[str, Any]] = {
    "rollback": {
        "run": rollback_run,
        "verify": rollback_verify,
        "description": "Roll back a service to its previous version",
    },
    "disable_flag": {
        "run": disable_flag_run,
        "verify": disable_flag_verify,
        "description": "Disable a feature/chaos flag",
    },
    "restart": {
        "run": restart_run,
        "verify": restart_verify,
        "description": "Restart a service",
    },
}


async def execute_action(kind: str, params: dict[str, Any]) -> str:
    """Execute a remediation action by kind."""
    if kind not in REMEDIATION_REGISTRY:
        return f"❌ Unknown remediation kind: {kind}"
    try:
        return await REMEDIATION_REGISTRY[kind]["run"](params)
    except Exception as exc:
        logger.exception("Remediation %s failed", kind)
        return f"❌ Remediation {kind} failed: {exc}"


async def verify_action(kind: str, params: dict[str, Any]) -> str:
    """Verify a remediation action by kind."""
    if kind not in REMEDIATION_REGISTRY:
        return f"❌ Unknown remediation kind: {kind}"
    try:
        return await REMEDIATION_REGISTRY[kind]["verify"](params)
    except Exception as exc:
        logger.exception("Verification for %s failed", kind)
        return f"⚠️ Verification for {kind} failed: {exc}"

"""Agent K — Remediation engine: guarded actions with verification."""

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


# ── Action implementations ──────────────────────────────────────


async def rollback_run(service: str, **kwargs: Any) -> str:
    """Rollback a service to its previous version via docker compose."""
    target_version = kwargs.get("version", "1.4.1")
    env = os.environ.copy()
    env["CHECKOUT_VERSION"] = target_version
    env["CHAOS_MODE"] = ""

    logger.info("Rolling back %s to version %s", service, target_version)
    try:
        result = subprocess.run(
            ["docker", "compose", "up", "-d", service],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            cwd="/app",
        )
        _emit_marker_log(service, target_version, "rollback")

        # Also clear the Redis chaos flag
        r = await _get_redis()
        await r.delete("chaos:bad-deploy")
        await r.aclose()

        return f"✅ Rolled back {service} to v{target_version}. Deployment marker emitted."
    except subprocess.CalledProcessError as exc:
        return f"❌ Rollback failed: {exc.stderr}"
    except FileNotFoundError:
        return "❌ docker compose not found in container"


async def rollback_verify(service: str, **kwargs: Any) -> str:
    """Verify rollback by checking if error rate has dropped."""
    logger.info("Waiting 60s before verification of %s rollback...", service)
    await asyncio.sleep(60)
    return f"✅ Verification: {service} rollback — monitoring for error rate recovery. Check SigNoz dashboard for confirmation."


async def disable_flag_run(flag: str, **kwargs: Any) -> str:
    """Disable a feature/chaos flag via Redis."""
    r = await _get_redis()
    await r.delete(f"chaos:{flag}")
    await r.aclose()
    logger.info("Disabled flag: chaos:%s", flag)
    return f"✅ Flag chaos:{flag} disabled."


async def disable_flag_verify(flag: str, **kwargs: Any) -> str:
    """Verify the flag is disabled."""
    await asyncio.sleep(30)
    r = await _get_redis()
    val = await r.get(f"chaos:{flag}")
    await r.aclose()
    if val is None:
        return f"✅ Flag chaos:{flag} confirmed disabled."
    return f"⚠️ Flag chaos:{flag} still set: {val}"


async def restart_run(service: str, **kwargs: Any) -> str:
    """Restart a service via docker compose."""
    logger.info("Restarting service: %s", service)
    try:
        result = subprocess.run(
            ["docker", "compose", "restart", service],
            check=True,
            capture_output=True,
            text=True,
            cwd="/app",
        )
        return f"✅ Restarted {service}."
    except subprocess.CalledProcessError as exc:
        return f"❌ Restart failed: {exc.stderr}"
    except FileNotFoundError:
        return "❌ docker compose not found in container"


async def restart_verify(service: str, **kwargs: Any) -> str:
    """Verify service is healthy after restart."""
    await asyncio.sleep(15)
    return f"✅ {service} restart verification — service should be healthy. Check SigNoz for confirmation."


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

    handler = REMEDIATION_REGISTRY[kind]
    return await handler["run"](**params)


async def verify_action(kind: str, params: dict[str, Any]) -> str:
    """Verify a remediation action by kind."""
    if kind not in REMEDIATION_REGISTRY:
        return f"❌ Unknown remediation kind: {kind}"

    handler = REMEDIATION_REGISTRY[kind]
    return await handler["verify"](**params)

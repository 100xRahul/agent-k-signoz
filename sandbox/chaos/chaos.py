"""
Chaos CLI – trigger and resolve chaos scenarios.

Usage:
    python -m chaos bad-deploy
    python -m chaos pool-exhaustion
    python -m chaos flag-combo
    python -m chaos secret-leak
    python -m chaos resolve

All scenarios are driven by Redis flags that the sandbox services check
per-request — no container restarts needed. A "deploy" is modeled by the
`chaos:checkout-version` key (checkout stamps it on every span), paired
with a deployment-marker log emitted from service.name=deploy-bot.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

import redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("chaos")

REDIS_URL = os.getenv("REDIS_URL", "redis://sandbox-redis:6379")
OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://signoz-otel-collector:4317")

BASELINE_VERSION = "1.4.1"
BAD_VERSION = "1.4.2"

SCENARIOS = {"bad-deploy", "pool-exhaustion", "flag-combo", "secret-leak", "resolve"}


def _redis_client() -> redis.Redis:
    """Create a synchronous Redis client."""
    return redis.from_url(REDIS_URL, decode_responses=True)


def _emit_deployment_marker(service: str, version: str, event: str = "deployment") -> None:
    """Emit a deployment/rollback marker log line via OTLP.

    Uses the OTel SDK directly to send a single log record from service.name=deploy-bot.
    """
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter

        resource = Resource.create({"service.name": "deploy-bot"})
        log_provider = LoggerProvider(resource=resource)
        log_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(endpoint=OTEL_ENDPOINT, insecure=True)
            )
        )
        handler = LoggingHandler(level=logging.INFO, logger_provider=log_provider)

        deploy_logger = logging.getLogger("deploy-bot")
        deploy_logger.addHandler(handler)
        deploy_logger.setLevel(logging.INFO)

        deploy_logger.info(
            json.dumps({
                "event": event,
                "service": service,
                "version": version,
                "timestamp": time.time(),
            })
        )

        # Flush to ensure the log is sent
        log_provider.force_flush()
        log_provider.shutdown()
        logger.info("Marker emitted: %s %s v%s", event, service, version)
    except ImportError:
        logger.warning(
            "OTel SDK not available – skipping deployment marker. "
            "Install opentelemetry-sdk and opentelemetry-exporter-otlp-proto-grpc."
        )


def trigger_bad_deploy() -> None:
    """Trigger the bad-deploy scenario."""
    r = _redis_client()
    r.set("chaos:checkout-version", BAD_VERSION)
    r.set("chaos:bad-deploy", "1")
    logger.info("Redis: chaos:checkout-version=%s, chaos:bad-deploy=1", BAD_VERSION)

    _emit_deployment_marker(service="checkout", version=BAD_VERSION)

    logger.info(
        "🔥 bad-deploy scenario ACTIVE – checkout v%s with +800ms latency and 35%% error rate",
        BAD_VERSION,
    )


def trigger_pool_exhaustion() -> None:
    """Trigger the pool-exhaustion scenario."""
    r = _redis_client()
    r.set("chaos:pool-exhaustion", "1")
    logger.info("🔥 pool-exhaustion scenario ACTIVE – payment connections held for 5s")


def trigger_flag_combo() -> None:
    """Trigger the flag-combo scenario."""
    r = _redis_client()
    r.set("chaos:flag-combo", "1")
    logger.info("🔥 flag-combo scenario ACTIVE – new-checkout + express-pay = 25%% failure")


def trigger_secret_leak() -> None:
    """Trigger the secret-leak scenario."""
    r = _redis_client()
    r.set("chaos:secret-leak", "1")
    logger.info("🔥 secret-leak scenario ACTIVE – fake AKIA keys appearing in payment logs")


def resolve_all() -> None:
    """Clear all chaos flags and restore checkout to baseline."""
    r = _redis_client()

    for flag in ["bad-deploy", "pool-exhaustion", "flag-combo", "secret-leak"]:
        r.delete(f"chaos:{flag}")
        logger.info("Cleared chaos:%s", flag)

    r.set("chaos:checkout-version", BASELINE_VERSION)
    _emit_deployment_marker(service="checkout", version=BASELINE_VERSION, event="rollback")

    logger.info("✅ All chaos scenarios RESOLVED – checkout restored to v%s", BASELINE_VERSION)


DISPATCH: dict[str, callable] = {
    "bad-deploy": trigger_bad_deploy,
    "pool-exhaustion": trigger_pool_exhaustion,
    "flag-combo": trigger_flag_combo,
    "secret-leak": trigger_secret_leak,
    "resolve": resolve_all,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in SCENARIOS:
        print(f"Usage: python -m chaos <{'|'.join(sorted(SCENARIOS))}>")
        sys.exit(1)

    scenario = sys.argv[1]
    logger.info("Triggering scenario: %s", scenario)
    DISPATCH[scenario]()


if __name__ == "__main__":
    main()

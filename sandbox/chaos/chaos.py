"""
Chaos CLI – trigger and resolve chaos scenarios.

Usage:
    python -m chaos bad-deploy
    python -m chaos pool-exhaustion
    python -m chaos flag-combo
    python -m chaos secret-leak
    python -m chaos resolve
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time

import redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("chaos")

REDIS_URL = os.getenv("REDIS_URL", "redis://sandbox-redis:6379")
OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://signoz-otel-collector:4317")

SCENARIOS = {"bad-deploy", "pool-exhaustion", "flag-combo", "secret-leak", "resolve"}


def _redis_client() -> redis.Redis:
    """Create a synchronous Redis client."""
    return redis.from_url(REDIS_URL, decode_responses=True)


def _emit_deployment_marker(service: str, version: str) -> None:
    """Emit a deployment marker log line via OTLP.

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
                "event": "deployment",
                "service": service,
                "version": version,
                "timestamp": time.time(),
            })
        )

        # Flush to ensure the log is sent
        log_provider.force_flush()
        log_provider.shutdown()
        logger.info("Deployment marker emitted: %s v%s", service, version)
    except ImportError:
        logger.warning(
            "OTel SDK not available – skipping deployment marker. "
            "Install opentelemetry-sdk and opentelemetry-exporter-otlp-proto-grpc."
        )


def _docker_compose_restart_checkout(version: str, chaos_mode: str = "") -> None:
    """Restart the checkout container with updated env."""
    env = os.environ.copy()
    env["CHECKOUT_VERSION"] = version
    env["CHAOS_MODE"] = chaos_mode

    logger.info("Restarting checkout: version=%s chaos_mode=%s", version, chaos_mode)
    try:
        subprocess.run(
            ["docker", "compose", "up", "-d", "checkout"],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        logger.info("Checkout restarted successfully")
    except subprocess.CalledProcessError as exc:
        logger.error("Failed to restart checkout: %s\n%s", exc, exc.stderr)
    except FileNotFoundError:
        logger.warning("docker compose not found – skipping container restart")


def trigger_bad_deploy() -> None:
    """Trigger the bad-deploy scenario."""
    r = _redis_client()
    r.set("chaos:bad-deploy", "1")
    logger.info("Redis flag chaos:bad-deploy SET")

    # Restart checkout with bad version
    _docker_compose_restart_checkout(version="1.4.2", chaos_mode="bad-deploy")

    # Emit deployment marker
    _emit_deployment_marker(service="checkout", version="1.4.2")

    logger.info("🔥 bad-deploy scenario ACTIVE – checkout v1.4.2 with +800ms latency and 35%% error rate")


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

    # Clear all chaos flags
    for flag in ["bad-deploy", "pool-exhaustion", "flag-combo", "secret-leak"]:
        r.delete(f"chaos:{flag}")
        logger.info("Cleared chaos:%s", flag)

    # Restart checkout at baseline version
    _docker_compose_restart_checkout(version="1.4.1", chaos_mode="")

    logger.info("✅ All chaos scenarios RESOLVED – checkout restored to v1.4.1")


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

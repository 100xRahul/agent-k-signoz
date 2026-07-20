"""Checkout service – orchestrates payment + inventory reservation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import uuid
from typing import Any

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace

from telemetry import setup_telemetry

app = FastAPI(title="checkout")
tracer = setup_telemetry(app)
logger = logging.getLogger("checkout")

PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment:8003")
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://inventory:8004")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
SERVICE_VERSION = os.getenv("SERVICE_VERSION", "1.4.1")
CHAOS_MODE_ENV = os.getenv("CHAOS_MODE", "")

redis_client: aioredis.Redis | None = None


@app.on_event("startup")
async def _startup() -> None:
    global redis_client
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)


@app.on_event("shutdown")
async def _shutdown() -> None:
    if redis_client:
        await redis_client.aclose()


# ── Chaos helpers ──────────────────────────────────────────────────

async def _redis_flag(flag: str) -> bool:
    """Return True if a Redis chaos flag is set."""
    if redis_client is None:
        return False
    try:
        val = await redis_client.get(f"chaos:{flag}")
        return val is not None and val != ""
    except Exception:
        return False


async def _is_chaos(scenario: str) -> bool:
    """Check both env var AND Redis flag."""
    if CHAOS_MODE_ENV == scenario:
        return True
    return await _redis_flag(scenario)


async def _current_version() -> str:
    """Deployed version comes from Redis so a 'deploy' needs no container restart."""
    if redis_client is not None:
        try:
            val = await redis_client.get("chaos:checkout-version")
            if val:
                return val
        except Exception:
            pass
    return SERVICE_VERSION


# ── Main endpoint ─────────────────────────────────────────────────

@app.post("/checkout")
async def checkout(request: Request) -> JSONResponse:
    """Process an order: call payment then reserve inventory."""
    body: dict[str, Any] = await request.json()

    order_id: str = body.get("order_id", str(uuid.uuid4()))
    user_id: str = body.get("user_id", "anonymous")
    tenant: str = body.get("tenant", "unknown")
    total: float = float(body.get("total", 0.0))
    feature_flags: list[str] = body.get("feature_flags", [])
    items: list[dict[str, Any]] = body.get("items", [])

    version = await _current_version()

    with tracer.start_as_current_span("checkout.process_order") as span:
        span.set_attribute("order.id", order_id)
        span.set_attribute("order.total", total)
        span.set_attribute("user.id", user_id)
        span.set_attribute("tenant.name", tenant)
        span.set_attribute("feature_flags", feature_flags)
        span.set_attribute("service.version", version)

        retry_count = 0

        # ── Chaos: bad-deploy ──────────────────────────────────
        if await _is_chaos("bad-deploy"):
            span.set_attribute("chaos.scenario", "bad-deploy")
            # Add +800ms latency
            await asyncio.sleep(0.8)
            # Fail 35% of orders
            if random.random() < 0.35:
                span.set_attribute("has_error", True)
                span.set_status(trace.StatusCode.ERROR, "bad-deploy: simulated failure")
                logger.error(
                    json.dumps({
                        "event": "order_processed",
                        "order_id": order_id,
                        "retry_count": retry_count,
                        "consumer_group": "orders-cg",
                        "status": "error",
                        "error": "internal checkout failure (bad-deploy)",
                        "service_version": version,
                    })
                )
                return JSONResponse(
                    status_code=500,
                    content={"error": "internal checkout failure", "order_id": order_id},
                )

        # ── Chaos: flag-combo ──────────────────────────────────
        if await _is_chaos("flag-combo"):
            span.set_attribute("chaos.scenario", "flag-combo")
            if "new-checkout" in feature_flags and "express-pay" in feature_flags:
                if random.random() < 0.25:
                    span.set_attribute("has_error", True)
                    span.set_status(
                        trace.StatusCode.ERROR,
                        "flag-combo: new-checkout + express-pay conflict",
                    )
                    logger.error(
                        json.dumps({
                            "event": "order_processed",
                            "order_id": order_id,
                            "retry_count": retry_count,
                            "consumer_group": "orders-cg",
                            "status": "error",
                            "error": "feature flag conflict: new-checkout + express-pay",
                            "service_version": version,
                        })
                    )
                    return JSONResponse(
                        status_code=500,
                        content={
                            "error": "feature flag conflict",
                            "order_id": order_id,
                        },
                    )

        # ── Call payment service ───────────────────────────────
        async with httpx.AsyncClient(timeout=30.0) as client:
            pay_resp = await client.post(
                f"{PAYMENT_URL}/pay",
                json={
                    "order_id": order_id,
                    "amount": total,
                    "user_id": user_id,
                    "tenant": tenant,
                },
            )

        if pay_resp.status_code >= 400:
            span.set_attribute("has_error", True)
            span.set_status(trace.StatusCode.ERROR, "payment failed")
            logger.error(
                json.dumps({
                    "event": "order_processed",
                    "order_id": order_id,
                    "retry_count": retry_count,
                    "consumer_group": "orders-cg",
                    "status": "error",
                    "error": f"payment service returned {pay_resp.status_code}",
                    "service_version": version,
                })
            )
            return JSONResponse(
                status_code=pay_resp.status_code,
                content={"error": "payment failed", "order_id": order_id},
            )

        # ── Call inventory reserve ─────────────────────────────
        async with httpx.AsyncClient(timeout=10.0) as client:
            reserve_resp = await client.post(
                f"{INVENTORY_URL}/reserve",
                json={"order_id": order_id, "items": items},
            )

        if reserve_resp.status_code >= 400:
            span.set_attribute("has_error", True)
            span.set_status(trace.StatusCode.ERROR, "inventory reserve failed")

        # ── Success log ────────────────────────────────────────
        logger.info(
            json.dumps({
                "event": "order_processed",
                "order_id": order_id,
                "retry_count": retry_count,
                "consumer_group": "orders-cg",
                "status": "success",
                "total": total,
                "user_id": user_id,
                "tenant": tenant,
                "service_version": version,
            })
        )

        return JSONResponse(
            status_code=200,
            content={"status": "ok", "order_id": order_id, "total": total},
        )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": SERVICE_VERSION}

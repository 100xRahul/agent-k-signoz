"""Payment service – records payments in Postgres."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import uuid
from typing import Any

import asyncpg
import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace

from telemetry import setup_telemetry

app = FastAPI(title="payment")
tracer = setup_telemetry(app)
logger = logging.getLogger("payment")

PG_DSN = os.getenv(
    "PG_DSN", "postgresql://payments:payments123@localhost:5432/payments"
)
POOL_SIZE = int(os.getenv("POOL_SIZE", "10"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

pg_pool: asyncpg.Pool | None = None
redis_client: aioredis.Redis | None = None


@app.on_event("startup")
async def _startup() -> None:
    global pg_pool, redis_client
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)

    # Create the pool and ensure the payments table exists
    pg_pool = await asyncpg.create_pool(
        dsn=PG_DSN,
        min_size=2,
        max_size=POOL_SIZE,
    )
    async with pg_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                order_id    TEXT NOT NULL,
                amount      DOUBLE PRECISION NOT NULL,
                user_id     TEXT NOT NULL,
                tenant      TEXT NOT NULL,
                created_at  TIMESTAMPTZ DEFAULT now()
            );
        """)
    logger.info("Payment service started – PG pool size %d", POOL_SIZE)


@app.on_event("shutdown")
async def _shutdown() -> None:
    if pg_pool:
        await pg_pool.close()
    if redis_client:
        await redis_client.aclose()


async def _redis_flag(flag: str) -> bool:
    """Return True if a Redis chaos flag is set."""
    if redis_client is None:
        return False
    try:
        val = await redis_client.get(f"chaos:{flag}")
        return val is not None and val != ""
    except Exception:
        return False


@app.post("/pay")
async def pay(request: Request) -> JSONResponse:
    """Process a payment – insert into Postgres."""
    body: dict[str, Any] = await request.json()
    order_id: str = body.get("order_id", str(uuid.uuid4()))
    amount: float = float(body.get("amount", 0.0))
    user_id: str = body.get("user_id", "anonymous")
    tenant: str = body.get("tenant", "unknown")

    with tracer.start_as_current_span("payment.process") as span:
        span.set_attribute("order.id", order_id)
        span.set_attribute("payment.amount", amount)
        span.set_attribute("user.id", user_id)
        span.set_attribute("tenant.name", tenant)

        # ── Chaos: secret-leak ─────────────────────────────────
        if await _redis_flag("secret-leak"):
            span.set_attribute("chaos.scenario", "secret-leak")
            fake_key = "AKIAIOSFODNN7EXAMPLE"
            fake_jwt = (
                "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
                "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0."
                "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
            )
            logger.error(
                json.dumps(
                    {
                        "event": "payment_error",
                        "order_id": order_id,
                        "error": f"Auth failed for key={fake_key} jwt={fake_jwt}",
                        "aws_access_key_id": fake_key,
                        "token": fake_jwt,
                    }
                )
            )

        # ── Chaos: pool-exhaustion ─────────────────────────────
        if await _redis_flag("pool-exhaustion"):
            span.set_attribute("chaos.scenario", "pool-exhaustion")
            # Hold a connection for 5 seconds to starve the pool
            try:
                conn = await asyncio.wait_for(pg_pool.acquire(), timeout=2.0)
                try:
                    await asyncio.sleep(5.0)
                finally:
                    await pg_pool.release(conn)
            except Exception as exc:
                span.set_status(
                    trace.StatusCode.ERROR,
                    "pool exhaustion: timeout acquiring connection",
                )
                logger.error(
                    json.dumps(
                        {
                            "event": "payment_error",
                            "order_id": order_id,
                            "error": f"connection pool exhausted: {exc}",
                        }
                    )
                )
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "connection pool exhausted",
                        "order_id": order_id,
                    },
                )

        # ── Simulated work (30–80ms) ──────────────────────────
        await asyncio.sleep(random.uniform(0.03, 0.08))

        # ── Insert into Postgres ───────────────────────────────
        try:
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO payments (order_id, amount, user_id, tenant)
                    VALUES ($1, $2, $3, $4)
                    """,
                    order_id,
                    amount,
                    user_id,
                    tenant,
                )
        except Exception as exc:
            span.set_status(trace.StatusCode.ERROR, str(exc))
            logger.error(
                json.dumps(
                    {
                        "event": "payment_error",
                        "order_id": order_id,
                        "error": str(exc),
                    }
                )
            )
            return JSONResponse(
                status_code=500,
                content={"error": "payment insert failed", "order_id": order_id},
            )

        logger.info(
            json.dumps(
                {
                    "event": "payment_processed",
                    "order_id": order_id,
                    "amount": amount,
                    "user_id": user_id,
                    "tenant": tenant,
                }
            )
        )

        return JSONResponse(
            status_code=200,
            content={"status": "ok", "order_id": order_id, "amount": amount},
        )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

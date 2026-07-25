"""Load generator – sends a weighted mix of checkout and product requests."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import uuid
from typing import Any

import httpx
import redis.asyncio as aioredis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("loadgen")

TARGET = os.getenv("TARGET", "http://gateway:8001")
RPS = int(os.getenv("RPS", "5"))
SEED = int(os.getenv("SEED", "42"))
REDIS_URL = os.getenv("REDIS_URL", "redis://sandbox-redis:6379")

# ── Fake data ──────────────────────────────────────────────────────

TENANTS = ["M&M toys", "acme", "globex"]

USERS = [f"user-{i:03d}" for i in range(1, 21)]  # 20 fake users

FEATURE_FLAGS_POOL = ["new-checkout", "express-pay", "gift-wrap"]

PRODUCTS = [
    {"sku": "WIDGET-001", "quantity": 1},
    {"sku": "WIDGET-002", "quantity": 1},
    {"sku": "GADGET-001", "quantity": 1},
    {"sku": "GADGET-002", "quantity": 2},
    {"sku": "GIZMO-001", "quantity": 1},
    {"sku": "GIZMO-002", "quantity": 3},
    {"sku": "DOOHICK-001", "quantity": 1},
    {"sku": "DOOHICK-002", "quantity": 2},
]


def _random_flags(rng: random.Random, force_combo: bool = False) -> list[str]:
    """Return a random subset of feature flags.

    When flag-combo chaos is active, bias toward including BOTH new-checkout
    and express-pay so the scenario reliably triggers alerts within eval window.
    """
    if force_combo:
        extras = rng.sample(
            [f for f in FEATURE_FLAGS_POOL if f not in ("new-checkout", "express-pay")],
            k=rng.randint(0, 1),
        )
        return ["new-checkout", "express-pay", *extras]
    count = rng.randint(0, len(FEATURE_FLAGS_POOL))
    return rng.sample(FEATURE_FLAGS_POOL, count)


def _random_items(rng: random.Random) -> list[dict[str, Any]]:
    """Return 1–3 random product items."""
    count = rng.randint(1, 3)
    return rng.sample(PRODUCTS, count)


async def _send_checkout(
    client: httpx.AsyncClient, rng: random.Random, force_combo: bool = False
) -> None:
    """Send a checkout request."""
    user = rng.choice(USERS)
    tenant = rng.choice(TENANTS)
    total = round(rng.uniform(5.0, 500.0), 2)
    flags = _random_flags(rng, force_combo=force_combo)
    items = _random_items(rng)
    order_id = str(uuid.uuid4())

    payload = {
        "order_id": order_id,
        "user_id": user,
        "tenant": tenant,
        "total": total,
        "feature_flags": flags,
        "items": items,
    }

    try:
        resp = await client.post(f"{TARGET}/api/checkout", json=payload)
        logger.info(
            "checkout order=%s tenant=%s user=%s total=%.2f flags=%s status=%d",
            order_id[:8],
            tenant,
            user,
            total,
            flags,
            resp.status_code,
        )
    except Exception as exc:
        logger.warning("checkout request failed: %s", exc)


async def _send_products(client: httpx.AsyncClient, rng: random.Random) -> None:
    """Send a products listing request."""
    tenant = rng.choice(TENANTS)
    try:
        resp = await client.get(f"{TARGET}/api/products", params={"tenant": tenant})
        logger.info("products tenant=%s status=%d", tenant, resp.status_code)
    except Exception as exc:
        logger.warning("products request failed: %s", exc)


async def _flag_combo_active() -> bool:
    try:
        r = aioredis.from_url(REDIS_URL, decode_responses=True)
        val = await r.get("chaos:flag-combo")
        await r.aclose()
        return val is not None and val != ""
    except Exception:
        return False


async def _run() -> None:
    """Main load generation loop."""
    rng = random.Random(SEED)
    interval = 1.0 / RPS if RPS > 0 else 1.0

    logger.info("Load generator started – target=%s rps=%d seed=%d", TARGET, RPS, SEED)

    # Wait a bit for services to be ready
    await asyncio.sleep(5)

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            combo_active = await _flag_combo_active()
            # Weighted mix: 70% checkout, 30% products
            roll = rng.random()
            if roll < 0.70:
                # When flag-combo chaos is on, 60% of checkout traffic carries both flags
                force_combo = combo_active and rng.random() < 0.60
                asyncio.create_task(_send_checkout(client, rng, force_combo=force_combo))
            else:
                asyncio.create_task(_send_products(client, rng))

            await asyncio.sleep(interval)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()

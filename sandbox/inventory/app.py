"""Inventory service – product catalog and reservation via Redis."""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace

from telemetry import setup_telemetry

app = FastAPI(title="inventory")
tracer = setup_telemetry(app)
logger = logging.getLogger("inventory")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
redis_client: aioredis.Redis | None = None

# Seed products
SEED_PRODUCTS: list[dict[str, Any]] = [
    {"sku": "WIDGET-001", "name": "Turbo Widget", "price": 29.99, "stock": 500},
    {"sku": "WIDGET-002", "name": "Mega Widget", "price": 49.99, "stock": 300},
    {"sku": "GADGET-001", "name": "Super Gadget", "price": 99.99, "stock": 200},
    {"sku": "GADGET-002", "name": "Mini Gadget", "price": 14.99, "stock": 1000},
    {"sku": "GIZMO-001", "name": "Ultra Gizmo", "price": 199.99, "stock": 100},
    {"sku": "GIZMO-002", "name": "Pocket Gizmo", "price": 9.99, "stock": 2000},
    {"sku": "DOOHICK-001", "name": "Fancy Doohickey", "price": 74.50, "stock": 150},
    {"sku": "DOOHICK-002", "name": "Basic Doohickey", "price": 24.00, "stock": 800},
]


@app.on_event("startup")
async def _startup() -> None:
    global redis_client
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)

    # Seed product catalog into Redis
    for product in SEED_PRODUCTS:
        key = f"product:{product['sku']}"
        await redis_client.hset(key, mapping={
            "sku": product["sku"],
            "name": product["name"],
            "price": str(product["price"]),
            "stock": str(product["stock"]),
        })
    # Store the SKU index
    await redis_client.sadd("product:skus", *[p["sku"] for p in SEED_PRODUCTS])
    logger.info("Inventory service started – seeded %d products", len(SEED_PRODUCTS))


@app.on_event("shutdown")
async def _shutdown() -> None:
    if redis_client:
        await redis_client.aclose()


@app.get("/products")
async def list_products() -> list[dict[str, Any]]:
    """Return all products from Redis."""
    with tracer.start_as_current_span("inventory.list_products") as span:
        skus = await redis_client.smembers("product:skus")
        products: list[dict[str, Any]] = []
        for sku in sorted(skus):
            data = await redis_client.hgetall(f"product:{sku}")
            if data:
                products.append({
                    "sku": data["sku"],
                    "name": data["name"],
                    "price": float(data["price"]),
                    "stock": int(data["stock"]),
                })
        span.set_attribute("inventory.product_count", len(products))
        return products


@app.post("/reserve")
async def reserve(request: Request) -> JSONResponse:
    """Reserve inventory for an order."""
    body: dict[str, Any] = await request.json()
    order_id: str = body.get("order_id", str(uuid.uuid4()))
    items: list[dict[str, Any]] = body.get("items", [])

    with tracer.start_as_current_span("inventory.reserve") as span:
        span.set_attribute("order.id", order_id)
        span.set_attribute("inventory.items_count", len(items))

        reserved: list[str] = []
        for item in items:
            sku = item.get("sku", "")
            qty = int(item.get("quantity", 1))
            key = f"product:{sku}"

            current_stock = await redis_client.hget(key, "stock")
            if current_stock is not None and int(current_stock) >= qty:
                await redis_client.hincrby(key, "stock", -qty)
                reserved.append(sku)

        logger.info(
            json.dumps({
                "event": "inventory_reserved",
                "order_id": order_id,
                "reserved_skus": reserved,
                "requested_items": len(items),
            })
        )

        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "order_id": order_id,
                "reserved": reserved,
            },
        )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

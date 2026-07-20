"""Gateway service – routes traffic to checkout and inventory."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response

from telemetry import setup_telemetry

app = FastAPI(title="gateway")
tracer = setup_telemetry(app)
logger = logging.getLogger("gateway")

CHECKOUT_URL = os.getenv("CHECKOUT_URL", "http://checkout:8002")
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://inventory:8004")


@app.post("/api/checkout")
async def api_checkout(request: Request) -> dict[str, Any]:
    """Forward checkout requests to the checkout service."""
    body: dict[str, Any] = await request.json()
    tenant: str = body.get("tenant", "unknown")

    with tracer.start_as_current_span("gateway.checkout") as span:
        span.set_attribute("tenant.name", tenant)
        span.set_attribute("http.route", "/api/checkout")

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{CHECKOUT_URL}/checkout", json=body)

        if resp.status_code >= 400:
            span.set_attribute("has_error", True)
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type="application/json",
            )

        return resp.json()


@app.get("/api/products")
async def api_products(request: Request) -> list[dict[str, Any]]:
    """Forward product listing requests to the inventory service."""
    tenant: str = request.query_params.get("tenant", "unknown")

    with tracer.start_as_current_span("gateway.products") as span:
        span.set_attribute("tenant.name", tenant)
        span.set_attribute("http.route", "/api/products")

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{INVENTORY_URL}/products")

        return resp.json()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

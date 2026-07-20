"""Fallback REST wrapper tools — same signatures as tools_mcp.py.

Uses SigNoz REST API (query_range v5) via httpx when MCP is unavailable.
Same tool names so loop.py doesn't need to change.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from config import settings

logger = logging.getLogger(__name__)

# SigNoz API base (internal, for REST calls)
_SIGNOZ_API = settings.signoz_url.rstrip("/") + "/api"

# Headers for SigNoz API
def _headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
    }


def _ts_now_ns() -> int:
    """Current timestamp in nanoseconds."""
    return int(time.time() * 1_000_000_000)


def _ts_minutes_ago_ns(minutes: int) -> int:
    """Timestamp N minutes ago in nanoseconds."""
    return int((time.time() - minutes * 60) * 1_000_000_000)


# ── Tool implementations ─────────────────────────────────────────


async def signoz_list_services(**kwargs: Any) -> str:
    """List services reporting to SigNoz."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{_SIGNOZ_API}/v1/services",
            headers=_headers(),
            params={"start": _ts_minutes_ago_ns(60), "end": _ts_now_ns()},
        )
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


async def signoz_search_traces(**kwargs: Any) -> str:
    """Search traces with filters."""
    payload = _build_query_payload(
        data_source="traces",
        aggregate_operator="noop",
        filters=kwargs.get("filters", []),
        limit=kwargs.get("limit", 10),
        order_by=kwargs.get("order_by"),
    )
    return await _execute_query(payload)


async def signoz_aggregate_traces(**kwargs: Any) -> str:
    """Aggregate traces with Query Builder v5."""
    payload = _build_query_payload(
        data_source="traces",
        aggregate_operator=kwargs.get("aggregate_operator", "count"),
        aggregate_attribute=kwargs.get("aggregate_attribute"),
        filters=kwargs.get("filters", []),
        group_by=kwargs.get("group_by", []),
        order_by=kwargs.get("order_by"),
        limit=kwargs.get("limit"),
        having=kwargs.get("having"),
    )
    return await _execute_query(payload)


async def signoz_get_trace_details(**kwargs: Any) -> str:
    """Get details of a specific trace by trace ID."""
    trace_id = kwargs.get("trace_id", "")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{_SIGNOZ_API}/v3/traces/{trace_id}",
            headers=_headers(),
        )
        resp.raise_for_status()
        result = json.dumps(resp.json(), indent=2)
        if len(result) > 15000:
            result = result[:15000] + "\n... [truncated]"
        return result


async def signoz_search_logs(**kwargs: Any) -> str:
    """Search logs with filters."""
    payload = _build_query_payload(
        data_source="logs",
        aggregate_operator="noop",
        filters=kwargs.get("filters", []),
        limit=kwargs.get("limit", 20),
        order_by=kwargs.get("order_by"),
    )
    return await _execute_query(payload)


async def signoz_aggregate_logs(**kwargs: Any) -> str:
    """Aggregate logs."""
    payload = _build_query_payload(
        data_source="logs",
        aggregate_operator=kwargs.get("aggregate_operator", "count"),
        aggregate_attribute=kwargs.get("aggregate_attribute"),
        filters=kwargs.get("filters", []),
        group_by=kwargs.get("group_by", []),
        order_by=kwargs.get("order_by"),
        limit=kwargs.get("limit"),
    )
    return await _execute_query(payload)


async def signoz_query_metrics(**kwargs: Any) -> str:
    """Query metrics."""
    payload = _build_query_payload(
        data_source="metrics",
        aggregate_operator=kwargs.get("aggregate_operator", "avg"),
        aggregate_attribute=kwargs.get("aggregate_attribute"),
        filters=kwargs.get("filters", []),
        group_by=kwargs.get("group_by", []),
    )
    return await _execute_query(payload)


async def signoz_execute_builder_query(**kwargs: Any) -> str:
    """Execute a raw Query Builder v5 query."""
    payload = kwargs.get("query", kwargs)
    if isinstance(payload, str):
        payload = json.loads(payload)
    return await _execute_query(payload)


async def signoz_get_alert(**kwargs: Any) -> str:
    """Get alert rule details."""
    alert_id = kwargs.get("alert_id", kwargs.get("id", ""))
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{_SIGNOZ_API}/v1/rules/{alert_id}",
            headers=_headers(),
        )
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


async def signoz_get_alert_history(**kwargs: Any) -> str:
    """Get alert firing history."""
    alert_id = kwargs.get("alert_id", kwargs.get("id", ""))
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{_SIGNOZ_API}/v1/rules/{alert_id}/history",
            headers=_headers(),
            params={
                "start": _ts_minutes_ago_ns(60),
                "end": _ts_now_ns(),
            },
        )
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


# ── Query Builder helpers ─────────────────────────────────────────


def _build_query_payload(
    data_source: str,
    aggregate_operator: str,
    aggregate_attribute: dict[str, Any] | str | None = None,
    filters: list[dict[str, Any]] | None = None,
    group_by: list[dict[str, Any]] | None = None,
    order_by: dict[str, Any] | list[dict[str, Any]] | None = None,
    limit: int | None = None,
    having: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a SigNoz Query Builder v5 payload."""
    now_ns = _ts_now_ns()
    start_ns = _ts_minutes_ago_ns(30)

    # Normalize aggregate_attribute
    agg_attr: dict[str, Any] = {}
    if isinstance(aggregate_attribute, dict):
        agg_attr = aggregate_attribute
    elif isinstance(aggregate_attribute, str) and aggregate_attribute:
        agg_attr = {"key": aggregate_attribute, "type": "tag", "dataType": "string"}

    # Build filter items
    filter_items: list[dict[str, Any]] = []
    if filters:
        for f in filters:
            if isinstance(f, dict):
                filter_items.append(f)

    # Build group by
    group_by_items: list[dict[str, Any]] = []
    if group_by:
        for g in group_by:
            if isinstance(g, dict):
                group_by_items.append(g)
            elif isinstance(g, str):
                group_by_items.append(
                    {"key": g, "type": "tag", "dataType": "string"}
                )

    # Build order by
    order_by_items: list[dict[str, Any]] = []
    if order_by:
        if isinstance(order_by, dict):
            order_by_items = [order_by]
        elif isinstance(order_by, list):
            order_by_items = order_by

    builder_query: dict[str, Any] = {
        "queryName": "A",
        "dataSource": data_source,
        "aggregateOperator": aggregate_operator,
        "aggregateAttribute": agg_attr,
        "filters": {
            "op": "AND",
            "items": filter_items,
        },
        "groupBy": group_by_items,
        "orderBy": order_by_items,
        "expression": "A",
        "disabled": False,
        "reduceTo": "sum",
    }

    if limit is not None:
        builder_query["limit"] = limit
    if having:
        builder_query["having"] = having

    return {
        "start": start_ns,
        "end": now_ns,
        "step": 60,
        "compositeQuery": {
            "queryType": "builder",
            "panelType": "table",
            "builderQueries": {"A": builder_query},
        },
    }


async def _execute_query(payload: dict[str, Any]) -> str:
    """Execute a query_range v5 request against SigNoz."""
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{_SIGNOZ_API}/v3/query_range",
            headers=_headers(),
            json=payload,
        )
        resp.raise_for_status()
        result = json.dumps(resp.json(), indent=2)
        if len(result) > 15000:
            result = result[:15000] + "\n... [truncated]"
        return result


# ── OpenAI tool format (same interface as tools_mcp) ─────────────

REST_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "signoz_list_services",
            "description": "List all services reporting telemetry to SigNoz.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "signoz_search_traces",
            "description": "Search traces with filters. Returns matching trace spans.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filters": {
                        "type": "array",
                        "description": "List of filter objects with key, op, value, type, dataType",
                        "items": {"type": "object"},
                    },
                    "limit": {"type": "integer", "description": "Max results", "default": 10},
                    "order_by": {"type": "object", "description": "Order by specification"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "signoz_aggregate_traces",
            "description": "Aggregate traces using Query Builder. Supports count, avg, sum, p50, p90, p95, p99, rate, countIf, sumIf etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "aggregate_operator": {"type": "string", "description": "Aggregation operator"},
                    "aggregate_attribute": {
                        "description": "Attribute to aggregate on (string name or {key, type, dataType} object)",
                    },
                    "filters": {"type": "array", "items": {"type": "object"}},
                    "group_by": {"type": "array", "items": {}},
                    "order_by": {"description": "Order by specification"},
                    "limit": {"type": "integer"},
                    "having": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["aggregate_operator"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "signoz_get_trace_details",
            "description": "Get full details of a specific trace by its trace ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "trace_id": {"type": "string", "description": "The trace ID to look up"},
                },
                "required": ["trace_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "signoz_search_logs",
            "description": "Search logs with filters. Supports JSON body predicates, regex, NOT EXISTS, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filters": {"type": "array", "items": {"type": "object"}},
                    "limit": {"type": "integer", "default": 20},
                    "order_by": {"type": "object"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "signoz_aggregate_logs",
            "description": "Aggregate logs with count, rate, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "aggregate_operator": {"type": "string"},
                    "aggregate_attribute": {},
                    "filters": {"type": "array", "items": {"type": "object"}},
                    "group_by": {"type": "array", "items": {}},
                    "order_by": {},
                    "limit": {"type": "integer"},
                },
                "required": ["aggregate_operator"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "signoz_query_metrics",
            "description": "Query metrics with aggregation and filtering.",
            "parameters": {
                "type": "object",
                "properties": {
                    "aggregate_operator": {"type": "string"},
                    "aggregate_attribute": {},
                    "filters": {"type": "array", "items": {"type": "object"}},
                    "group_by": {"type": "array", "items": {}},
                },
                "required": ["aggregate_operator"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "signoz_execute_builder_query",
            "description": "Execute a raw SigNoz Query Builder v5 query. Full control over the query payload.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "object", "description": "Full query_range v5 payload"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "signoz_get_alert",
            "description": "Get details of a specific SigNoz alert rule.",
            "parameters": {
                "type": "object",
                "properties": {
                    "alert_id": {"type": "string", "description": "Alert rule ID"},
                },
                "required": ["alert_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "signoz_get_alert_history",
            "description": "Get recent firing history for a SigNoz alert rule.",
            "parameters": {
                "type": "object",
                "properties": {
                    "alert_id": {"type": "string", "description": "Alert rule ID"},
                },
                "required": ["alert_id"],
            },
        },
    },
]


# Tool dispatcher (same interface as MCPClient.call_tool)
_TOOL_FUNCTIONS: dict[str, Any] = {
    "signoz_list_services": signoz_list_services,
    "signoz_search_traces": signoz_search_traces,
    "signoz_aggregate_traces": signoz_aggregate_traces,
    "signoz_get_trace_details": signoz_get_trace_details,
    "signoz_search_logs": signoz_search_logs,
    "signoz_aggregate_logs": signoz_aggregate_logs,
    "signoz_query_metrics": signoz_query_metrics,
    "signoz_execute_builder_query": signoz_execute_builder_query,
    "signoz_get_alert": signoz_get_alert,
    "signoz_get_alert_history": signoz_get_alert_history,
}


async def call_tool_rest(name: str, args: dict[str, Any]) -> str:
    """Dispatch a tool call to the REST implementation."""
    func = _TOOL_FUNCTIONS.get(name)
    if func is None:
        return f"Unknown tool: {name}"
    try:
        return await func(**args)
    except Exception as exc:
        return f"REST tool '{name}' failed: {exc}"


def get_rest_openai_tools() -> list[dict[str, Any]]:
    """Get tool definitions in OpenAI function-calling format."""
    return REST_TOOLS

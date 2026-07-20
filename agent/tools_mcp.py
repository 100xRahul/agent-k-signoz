"""MCP client — connects to SigNoz MCP server via streamable HTTP transport."""

from __future__ import annotations

import logging
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from config import settings

logger = logging.getLogger(__name__)

# ── Curated allowlist of MCP tools (10 tools) ────────────────────

TOOL_ALLOWLIST: set[str] = {
    "signoz_list_services",
    "signoz_aggregate_traces",
    "signoz_search_traces",
    "signoz_get_trace_details",
    "signoz_search_logs",
    "signoz_aggregate_logs",
    "signoz_query_metrics",
    "signoz_execute_builder_query",
    "signoz_get_alert",
    "signoz_get_alert_history",
}


class MCPClient:
    """MCP client for SigNoz MCP server using streamable HTTP."""

    def __init__(self) -> None:
        self._mcp_url: str = settings.mcp_url.rstrip("/") + "/mcp"
        self._tools_cache: list[dict[str, Any]] | None = None
        self._tool_schemas: dict[str, dict[str, Any]] = {}

    async def list_tools(self) -> list[dict[str, Any]]:
        """Fetch available tools from MCP server, filtered to allowlist."""
        if self._tools_cache is not None:
            return self._tools_cache

        tools: list[dict[str, Any]] = []
        try:
            async with streamablehttp_client(self._mcp_url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()

                    for tool in result.tools:
                        if tool.name in TOOL_ALLOWLIST:
                            schema = {
                                "name": tool.name,
                                "description": tool.description or "",
                                "input_schema": (
                                    tool.inputSchema
                                    if isinstance(tool.inputSchema, dict)
                                    else {}
                                ),
                            }
                            tools.append(schema)
                            self._tool_schemas[tool.name] = schema

            self._tools_cache = tools
            logger.info("Loaded %d MCP tools (filtered from allowlist)", len(tools))
        except Exception:
            logger.exception("Failed to list MCP tools")
            self._tools_cache = []

        return self._tools_cache

    async def call_tool(self, name: str, args: dict[str, Any]) -> str:
        """Execute an MCP tool call and return the result as a string."""
        try:
            async with streamablehttp_client(self._mcp_url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(name, args)

                    # Combine all content parts
                    parts: list[str] = []
                    for content in result.content:
                        if hasattr(content, "text"):
                            parts.append(content.text)
                        else:
                            parts.append(str(content))

                    output = "\n".join(parts)
                    # Truncate to 15k chars as per spec
                    if len(output) > 15000:
                        output = output[:15000] + "\n... [truncated]"
                    return output

        except Exception as exc:
            error_msg = f"MCP tool call '{name}' failed: {exc}"
            logger.exception(error_msg)
            return error_msg

    async def get_openai_tools(self) -> list[dict[str, Any]]:
        """Convert MCP tool schemas to OpenAI function-calling format."""
        mcp_tools = await self.list_tools()
        openai_tools: list[dict[str, Any]] = []

        for tool in mcp_tools:
            input_schema = tool.get("input_schema", {})
            # Ensure it has proper JSON Schema structure
            parameters: dict[str, Any] = {
                "type": "object",
                "properties": input_schema.get("properties", {}),
            }
            if "required" in input_schema:
                parameters["required"] = input_schema["required"]

            openai_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": parameters,
                    },
                }
            )

        return openai_tools


# Singleton
mcp_client = MCPClient()

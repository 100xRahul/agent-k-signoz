"""Core agent loop — hand-rolled tool-use loop over OpenAI chat-completions."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from openai import OpenAI

from config import settings
from models import InvestigationTrigger, RemediationAction
from playbook import PLAYBOOK, render_trigger
from report import rewrite_signoz_links
from slack import post_rca, post_remediation_proposal
from store import store
from telemetry import (
    get_current_trace_id,
    investigation_span,
    llm_call_span,
    record_cost,
    record_investigation_duration,
    record_investigation_started,
    set_investigation_result,
    set_llm_usage,
    set_tool_result,
    tool_call_span,
)

logger = logging.getLogger(__name__)


# ── Synthetic tools (not MCP-backed) ─────────────────────────────

FINISH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "finish_investigation",
        "description": "Finish the investigation with a final report. Call this when you have gathered enough evidence.",
        "parameters": {
            "type": "object",
            "properties": {
                "report_markdown": {
                    "type": "string",
                    "description": "Full investigation report in markdown, following the report template.",
                },
                "root_cause_oneliner": {
                    "type": "string",
                    "description": "One-sentence root cause summary.",
                },
                "confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "description": "Confidence level in the root cause.",
                },
            },
            "required": ["report_markdown", "root_cause_oneliner", "confidence"],
        },
    },
}

REMEDIATION_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "propose_remediation",
        "description": "Propose a remediation action for the identified root cause. Only allowed kinds: rollback, disable_flag, restart.",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["rollback", "disable_flag", "restart"],
                    "description": "Type of remediation action.",
                },
                "service": {
                    "type": "string",
                    "description": "Service name (for rollback/restart) or flag name (for disable_flag).",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this remediation should fix the issue.",
                },
            },
            "required": ["kind", "service", "reason"],
        },
    },
}


# ── Tool router ───────────────────────────────────────────────────

async def _get_tools() -> tuple[list[dict[str, Any]], Any]:
    """Get tool definitions and the tool caller (MCP or REST fallback)."""
    # Try MCP first
    try:
        from tools_mcp import mcp_client
        openai_tools = await mcp_client.get_openai_tools()
        if openai_tools:
            logger.info("Using MCP tools (%d tools loaded)", len(openai_tools))
            return openai_tools, mcp_client
    except Exception:
        logger.warning("MCP tools unavailable, falling back to REST")

    # Fallback to REST
    from tools_rest import get_rest_openai_tools
    return get_rest_openai_tools(), None


async def _call_tool(
    name: str,
    args: dict[str, Any],
    mcp_client: Any | None,
) -> str:
    """Route a tool call to MCP or REST fallback."""
    if mcp_client is not None:
        return await mcp_client.call_tool(name, args)
    else:
        from tools_rest import call_tool_rest
        return await call_tool_rest(name, args)


# ── Main investigation loop ──────────────────────────────────────


async def run_investigation(
    trigger: InvestigationTrigger,
    investigation_id: str,
) -> None:
    """Run the full agent investigation loop."""
    start_time = time.time()
    alertname = trigger.alertname

    record_investigation_started(alertname)

    with investigation_span(trigger.type.value, alertname) as inv_span:
        trace_id = get_current_trace_id()

        # Update investigation with trace_id
        store.update_investigation(investigation_id, trace_id=trace_id)

        # Build LLM client
        client = OpenAI(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
        )

        # Get tools
        sigtools, mcp = await _get_tools()
        all_tools = sigtools + [FINISH_TOOL, REMEDIATION_TOOL]

        # Initialize messages
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": PLAYBOOK},
            {"role": "user", "content": render_trigger(trigger.model_dump())},
        ]

        # Tracking
        total_cost = 0.0
        total_tokens_in = 0
        total_tokens_out = 0
        finished = False
        root_cause = ""
        confidence = ""
        report_md = ""

        try:
            for iteration in range(settings.max_iterations):
                logger.info(
                    "Investigation %s — iteration %d/%d (cost: $%.4f)",
                    investigation_id, iteration + 1, settings.max_iterations, total_cost,
                )

                # ── LLM call ──────────────────────────────────────
                with llm_call_span(settings.openai_model) as llm_span:
                    try:
                        response = client.chat.completions.create(
                            model=settings.openai_model,
                            messages=messages,
                            tools=all_tools,
                            tool_choice="auto",
                            temperature=0,
                        )
                    except Exception as exc:
                        logger.exception("LLM call failed at iteration %d", iteration)
                        set_llm_usage(llm_span, 0, 0, 0.0)
                        # Try to finish with what we have
                        report_md = f"# Investigation Aborted\n\nLLM call failed: {exc}"
                        root_cause = f"Investigation aborted: LLM error"
                        break

                    # Extract usage
                    usage = response.usage
                    input_tokens = usage.prompt_tokens if usage else 0
                    output_tokens = usage.completion_tokens if usage else 0
                    iter_cost = settings.compute_cost(input_tokens, output_tokens)

                    total_tokens_in += input_tokens
                    total_tokens_out += output_tokens
                    total_cost += iter_cost

                    set_llm_usage(llm_span, input_tokens, output_tokens, iter_cost)
                    record_cost(iter_cost, settings.openai_model)

                # ── Process response ──────────────────────────────
                choice = response.choices[0]
                message = choice.message

                # Append assistant message to history
                msg_dict: dict[str, Any] = {"role": "assistant"}
                if message.content:
                    msg_dict["content"] = message.content
                if message.tool_calls:
                    msg_dict["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in message.tool_calls
                    ]
                messages.append(msg_dict)

                # If no tool calls, the model is done talking
                if not message.tool_calls:
                    if message.content:
                        # Model responded without using finish_investigation
                        # Try to extract useful info
                        report_md = message.content
                        root_cause = "See report for details"
                    finished = True
                    break

                # ── Process tool calls ────────────────────────────
                for tool_call in message.tool_calls:
                    fn_name = tool_call.function.name
                    try:
                        fn_args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        fn_args = {}

                    logger.info(
                        "Tool call: %s(%s)",
                        fn_name,
                        json.dumps(fn_args)[:200],
                    )

                    # ── finish_investigation ──────────────────────
                    if fn_name == "finish_investigation":
                        report_md = fn_args.get("report_markdown", "")
                        root_cause = fn_args.get("root_cause_oneliner", "")
                        confidence = fn_args.get("confidence", "medium")

                        # Add cost footer to report
                        duration = time.time() - start_time
                        cost_footer = (
                            f"\n\n## Cost of This Investigation\n"
                            f"- {total_tokens_in + total_tokens_out} tokens "
                            f"({total_tokens_in} in + {total_tokens_out} out) · "
                            f"${total_cost:.4f} · {duration:.1f}s\n"
                            f"- [View my trace](signoz://trace/{trace_id})\n"
                        )
                        report_md += cost_footer

                        # Rewrite signoz:// links
                        report_md = rewrite_signoz_links(report_md)

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": "Investigation finished. Report saved.",
                        })
                        finished = True
                        break

                    # ── propose_remediation ───────────────────────
                    elif fn_name == "propose_remediation":
                        result = await _handle_propose_remediation(
                            investigation_id, fn_args
                        )
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": result,
                        })

                    # ── MCP / REST tools ──────────────────────────
                    else:
                        args_summary = json.dumps(fn_args)[:500]
                        with tool_call_span(fn_name, args_summary) as t_span:
                            result = await _call_tool(fn_name, fn_args, mcp)
                            result_bytes = len(result.encode("utf-8"))
                            is_error = "error" in result.lower()[:100] and "failed" in result.lower()[:200]
                            set_tool_result(t_span, result_bytes, is_error)

                        # Truncate result for context window
                        if len(result) > 15000:
                            result = result[:15000] + "\n... [truncated]"

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": result,
                        })

                if finished:
                    break

                # ── Budget check ──────────────────────────────────
                if total_cost > settings.max_cost_usd_per_investigation:
                    logger.warning(
                        "Investigation %s hit cost limit ($%.4f > $%.2f)",
                        investigation_id, total_cost, settings.max_cost_usd_per_investigation,
                    )
                    report_md = (
                        f"# ⚠️ Investigation Budget Exceeded\n\n"
                        f"Investigation was cut short at ${total_cost:.4f} "
                        f"(limit: ${settings.max_cost_usd_per_investigation:.2f}).\n\n"
                        f"## Partial Findings\n\n"
                        + (report_md or "No findings recorded before budget exceeded.")
                    )
                    root_cause = "Investigation budget exceeded — partial results"
                    break

        except Exception:
            logger.exception("Investigation %s failed", investigation_id)
            report_md = "# ❌ Investigation Failed\n\nAn unexpected error occurred during investigation."
            root_cause = "Investigation failed due to internal error"
            confidence = "low"

        # ── Persist results ───────────────────────────────────────
        duration = time.time() - start_time
        status = "done" if finished else "failed"

        if not report_md:
            report_md = "# Investigation completed without generating a report."
            root_cause = root_cause or "No root cause determined"

        store.update_investigation(
            investigation_id,
            status=status,
            finished_at=datetime.now(timezone.utc).isoformat(),
            report_md=report_md,
            root_cause=root_cause,
            cost_usd=total_cost,
            tokens_in=total_tokens_in,
            tokens_out=total_tokens_out,
        )

        # Set span attributes
        set_investigation_result(
            inv_span,
            root_cause=root_cause,
            confidence=confidence or "medium",
            total_cost_usd=total_cost,
        )
        record_investigation_duration(duration, alertname)

        # ── Notify Slack ──────────────────────────────────────────
        investigation = store.get_investigation(investigation_id)
        if investigation:
            try:
                await post_rca(investigation, report_md)
            except Exception:
                logger.exception("Failed to post RCA to Slack")

        logger.info(
            "Investigation %s completed: status=%s, cost=$%.4f, duration=%.1fs, root_cause=%s",
            investigation_id, status, total_cost, duration, root_cause[:100],
        )


async def _handle_propose_remediation(
    investigation_id: str,
    args: dict[str, Any],
) -> str:
    """Handle the propose_remediation synthetic tool call."""
    kind = args.get("kind", "restart")
    service = args.get("service", "")
    reason = args.get("reason", "")

    # Create action record
    params = {
        "kind": kind,
        "service": service,
        "reason": reason,
    }
    action_id = store.create_action(
        investigation_id=investigation_id,
        kind=kind,
        params_json=json.dumps(params),
    )

    # Post to Slack with approval link
    try:
        await post_remediation_proposal(
            investigation_id=investigation_id,
            action_id=action_id,
            kind=kind,
            service=service,
            reason=reason,
        )
    except Exception:
        logger.exception("Failed to post remediation proposal to Slack")

    # Auto-approve if configured
    if settings.auto_approve:
        logger.info("AUTO_APPROVE is on — executing remediation immediately")
        from remediation import execute_remediation
        store.update_action(action_id, status="approved")
        try:
            result = await execute_remediation(action_id)
            return f"Remediation auto-approved and executed: {result}"
        except Exception as exc:
            return f"Remediation auto-approved but execution failed: {exc}"

    return (
        f"Remediation proposed: {kind}({service}) — {reason}. "
        f"Action ID: {action_id}. "
        f"Awaiting human approval via Slack or /approve/{action_id} endpoint."
    )

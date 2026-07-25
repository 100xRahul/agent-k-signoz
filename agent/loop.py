"""Core agent loop — hand-rolled tool-use loop over OpenAI chat-completions."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from openai import AsyncOpenAI
from opentelemetry import trace

from auditor import AuditResult, audit_rca
from config import settings
from models import InvestigationTrigger
from playbook import PLAYBOOK, render_trigger
from report import prepend_audit_badge, rewrite_signoz_links
from slack import post_rca, post_remediation_proposal
from store import store
from telemetry import (
    get_current_trace_id,
    investigation_span,
    llm_call_span,
    record_audit,
    record_cost,
    record_investigation_duration,
    record_investigation_failed,
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


# ── Tool source ───────────────────────────────────────────────────


async def _get_tools() -> tuple[list[dict[str, Any]], Any]:
    """Get tool definitions from the SigNoz MCP server.

    No fallback: if the MCP server is unreachable the investigation fails
    loudly rather than degrading silently.
    """
    from tools_mcp import mcp_client

    openai_tools = await mcp_client.get_openai_tools()
    if not openai_tools:
        raise RuntimeError(
            f"SigNoz MCP server at {settings.mcp_url} returned no usable tools — "
            "check that the mcp-server container is running and SIGNOZ_API_KEY is valid."
        )
    logger.info("Using MCP tools (%d tools loaded)", len(openai_tools))
    return openai_tools, mcp_client


# ── Main investigation loop ──────────────────────────────────────


async def run_investigation(
    trigger: InvestigationTrigger,
    investigation_id: str,
) -> None:
    """Run the full agent investigation loop."""
    start_time = time.time()
    alertname = trigger.alertname

    record_investigation_started(alertname)
    scenario = trigger.labels.get("scenario", "")

    with investigation_span(trigger.type.value, alertname, scenario=scenario) as inv_span:
        trace_id = get_current_trace_id()

        # Update investigation with trace_id
        store.update_investigation(investigation_id, trace_id=trace_id)

        # Seal the opening of the investigation into the hash-chained ledger.
        store.append_ledger(
            investigation_id,
            "investigation.start",
            {
                "trigger_type": trigger.type.value,
                "alertname": alertname,
                "scenario": scenario,
                "trace_id": trace_id,
            },
        )

        # Async client — a blocking client here would freeze every other
        # endpoint (webhooks, approvals) for the duration of each LLM call.
        client = AsyncOpenAI(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
        )

        # Get tools — a failure here is fatal for the investigation, but must
        # still produce a failed record + report instead of a stuck "running" row.
        try:
            sigtools, _ = await _get_tools()
        except Exception as exc:
            logger.exception("Investigation %s could not load tools", investigation_id)
            store.update_investigation(
                investigation_id,
                status="failed",
                finished_at=datetime.now(timezone.utc).isoformat(),
                report_md=f"# ❌ Investigation Failed\n\nCould not load SigNoz tools: {exc}",
                root_cause="SigNoz MCP server unavailable",
            )
            set_investigation_result(
                inv_span,
                root_cause="SigNoz MCP server unavailable",
                confidence="low",
                total_cost_usd=0.0,
            )
            return
        all_tools = sigtools + [FINISH_TOOL, REMEDIATION_TOOL]

        # One MCP session for the whole investigation (~15 tool calls) instead
        # of a fresh HTTP handshake per call.
        from contextlib import AsyncExitStack

        from tools_mcp import mcp_client

        mcp_stack = AsyncExitStack()
        try:
            tool_session = await mcp_stack.enter_async_context(
                mcp_client.open_session()
            )
        except Exception as exc:
            logger.exception(
                "Investigation %s could not open MCP session", investigation_id
            )
            store.update_investigation(
                investigation_id,
                status="failed",
                finished_at=datetime.now(timezone.utc).isoformat(),
                report_md=f"# ❌ Investigation Failed\n\nCould not open MCP session: {exc}",
                root_cause="SigNoz MCP server unavailable",
            )
            set_investigation_result(
                inv_span,
                root_cause="SigNoz MCP server unavailable",
                confidence="low",
                total_cost_usd=0.0,
            )
            return

        # Initialize messages
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": PLAYBOOK},
            {"role": "user", "content": render_trigger(trigger.model_dump())},
        ]

        # Tracking
        total_cost = 0.0
        total_tokens_in = 0
        total_tokens_out = 0
        tools_used_count = 0
        finished = False
        root_cause = ""
        confidence = ""
        report_md = ""

        try:
            for iteration in range(settings.max_iterations):
                logger.info(
                    "Investigation %s — iteration %d/%d (cost: $%.4f)",
                    investigation_id,
                    iteration + 1,
                    settings.max_iterations,
                    total_cost,
                )

                # Two iterations before the cap, tell the model to wrap up so
                # every investigation ends with a real report, never a timeout.
                last_chance = iteration >= settings.max_iterations - 2
                if iteration == settings.max_iterations - 2:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "You are almost out of tool-call budget. Stop gathering "
                                "evidence and call finish_investigation NOW with your "
                                "best conclusion from the evidence you already have."
                            ),
                        }
                    )

                # ── LLM call ──────────────────────────────────────
                with llm_call_span(settings.openai_model) as llm_span:
                    try:
                        create_kwargs: dict[str, Any] = {
                            "model": settings.openai_model,
                            "messages": messages,
                            "tools": all_tools,
                            "tool_choice": (
                                {
                                    "type": "function",
                                    "function": {"name": "finish_investigation"},
                                }
                                if last_chance
                                else "auto"
                            ),
                        }
                        # gpt-5* reject an explicit temperature; only send it when configured.
                        if settings.llm_temperature is not None:
                            create_kwargs["temperature"] = settings.llm_temperature
                        response = await client.chat.completions.create(**create_kwargs)
                    except Exception as exc:
                        logger.exception("LLM call failed at iteration %d", iteration)
                        set_llm_usage(llm_span, 0, 0, 0.0)
                        # Try to finish with what we have
                        report_md = f"# Investigation Aborted\n\nLLM call failed: {exc}"
                        root_cause = "Investigation aborted: LLM error"
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
                        # Footer + link rewrite + audit badge are applied in the
                        # post-loop section so the cost footer can include the
                        # independent auditor's own token spend.

                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": "Investigation finished. Report saved.",
                            }
                        )
                        finished = True
                        break

                    # ── propose_remediation ───────────────────────
                    elif fn_name == "propose_remediation":
                        result = await _handle_propose_remediation(
                            investigation_id, fn_args
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": result,
                            }
                        )

                    # ── MCP / REST tools ──────────────────────────
                    else:
                        args_summary = json.dumps(fn_args)[:500]
                        with tool_call_span(fn_name, args_summary) as t_span:
                            result = await tool_session.call_tool(fn_name, fn_args)
                            result_bytes = len(result.encode("utf-8"))
                            is_error = result.startswith("MCP tool call") and (
                                "failed" in result
                            )
                            set_tool_result(t_span, result_bytes, is_error)
                        tools_used_count += 1

                        # Seal the tool call into the ledger — store a hash of the
                        # result (not the full body) so entries stay small but any
                        # later edit to what the agent "saw" is still detectable.
                        store.append_ledger(
                            investigation_id,
                            "tool.call",
                            {
                                "tool": fn_name,
                                "args": fn_args,
                                "result_bytes": result_bytes,
                                "result_sha256": hashlib.sha256(
                                    result.encode("utf-8")
                                ).hexdigest(),
                                "error": is_error,
                            },
                        )

                        # Truncate result for context window
                        if len(result) > 15000:
                            result = result[:15000] + "\n... [truncated]"

                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": result,
                            }
                        )

                if finished:
                    break

                # ── Budget check ──────────────────────────────────
                if total_cost > settings.max_cost_usd_per_investigation:
                    logger.warning(
                        "Investigation %s hit cost limit ($%.4f > $%.2f)",
                        investigation_id,
                        total_cost,
                        settings.max_cost_usd_per_investigation,
                    )
                    report_md = (
                        f"# ⚠️ Investigation Budget Exceeded\n\n"
                        f"Investigation was cut short at ${total_cost:.4f} "
                        f"(limit: ${settings.max_cost_usd_per_investigation:.2f}).\n\n"
                        f"## Partial Findings\n\n"
                        + (report_md or "No findings recorded before budget exceeded.")
                    )
                    root_cause = "Investigation budget exceeded — partial results"
                    # A partial report is still a delivered report.
                    finished = True
                    break

        except Exception:
            logger.exception("Investigation %s failed", investigation_id)
            report_md = "# ❌ Investigation Failed\n\nAn unexpected error occurred during investigation."
            root_cause = "Investigation failed due to internal error"
            confidence = "low"

        # ── Close MCP session ─────────────────────────────────────
        try:
            await mcp_stack.aclose()
        except Exception:
            logger.warning("MCP session close failed", exc_info=True)

        # ── Persist results ───────────────────────────────────────
        status = "done" if finished else "failed"

        if not report_md:
            report_md = "# Investigation completed without generating a report."
            root_cause = root_cause or "No root cause determined"

        # ── Independent auditor gate ──────────────────────────────
        # A second, independent model screens the finished RCA for groundedness
        # against the evidence the agent actually collected, BEFORE we publish.
        # Advisory, not blocking: an ungrounded RCA is still delivered (an SRE
        # needs the finding) but is badged and counted. Fail loud on error.
        audit = AuditResult(outcome="skipped")
        if status == "done" and settings.auditor_enabled:
            tool_evidence = "\n\n---\n\n".join(
                m.get("content", "")
                for m in messages
                if m.get("role") == "tool" and m.get("content")
            )
            # Give the auditor the trigger as ground truth so it doesn't flag the
            # alert name / fired-time (which come from the alert, not a tool call).
            context_block = (
                "## INVESTIGATION CONTEXT (the triggering alert — treat as ground truth)\n"
                + render_trigger(trigger.model_dump())
            )
            evidence = context_block + "\n\n---\n\n" + tool_evidence
            try:
                audit = await audit_rca(report_md, evidence)
            except Exception:
                logger.exception("Auditor pass raised unexpectedly")
                audit = AuditResult(outcome="error", error="auditor raised")
            total_cost += audit.cost_usd
            total_tokens_in += audit.tokens_in
            total_tokens_out += audit.tokens_out
            if audit.cost_usd:
                record_cost(audit.cost_usd, settings.effective_auditor_model)
            record_audit(audit.outcome)
            store.append_ledger(
                investigation_id,
                "audit.verdict",
                {
                    "outcome": audit.outcome,
                    "grounded": audit.grounded,
                    "score": audit.score,
                    "unsupported_claims": audit.unsupported_claims,
                    "auditor_model": settings.effective_auditor_model,
                },
            )

        # ── Finalize report: rewrite links, append cost footer, prepend badge ──
        duration = time.time() - start_time
        report_md = rewrite_signoz_links(report_md)
        report_md += (
            f"\n\n## Cost of This Investigation\n"
            f"- {total_tokens_in + total_tokens_out} tokens "
            f"({total_tokens_in} in + {total_tokens_out} out) · "
            f"${total_cost:.4f} · {duration:.1f}s "
            f"(incl. independent audit)\n"
            f"- [View my trace]({rewrite_signoz_links('signoz://trace/' + trace_id)})\n"
        )
        report_md = prepend_audit_badge(report_md, audit)

        # audit_grounded: 1 grounded / 0 ungrounded / NULL when skipped or errored.
        audit_grounded = (
            1 if audit.outcome == "grounded"
            else 0 if audit.outcome == "ungrounded"
            else None
        )

        store.update_investigation(
            investigation_id,
            status=status,
            finished_at=datetime.now(timezone.utc).isoformat(),
            report_md=report_md,
            root_cause=root_cause,
            cost_usd=total_cost,
            tokens_in=total_tokens_in,
            tokens_out=total_tokens_out,
            # Full audit trail: every prompt, tool call, and tool result.
            transcript_json=json.dumps(messages),
            audit_grounded=audit_grounded,
            audit_score=audit.score,
            audit_json=audit.to_json(),
        )

        # Seal the verdict into the ledger.
        store.append_ledger(
            investigation_id,
            "investigation.finish",
            {
                "status": status,
                "root_cause": root_cause,
                "confidence": confidence or "medium",
                "cost_usd": round(total_cost, 6),
                "audit_outcome": audit.outcome,
            },
        )

        # Set span attributes
        set_investigation_result(
            inv_span,
            root_cause=root_cause,
            confidence=confidence or "medium",
            total_cost_usd=total_cost,
            tools_used_count=tools_used_count,
            model=settings.openai_model,
            audit_outcome=audit.outcome,
            audit_score=audit.score,
        )
        record_investigation_duration(duration, alertname)
        if status == "failed":
            record_investigation_failed(alertname)
            inv_span.set_attribute("error", True)
            inv_span.set_status(trace.StatusCode.ERROR, root_cause[:200])

        # ── Notify Slack ──────────────────────────────────────────
        investigation = store.get_investigation(investigation_id)
        if investigation:
            try:
                await post_rca(investigation, report_md)
            except Exception:
                logger.exception("Failed to post RCA to Slack")

        logger.info(
            "Investigation %s completed: status=%s, cost=$%.4f, duration=%.1fs, root_cause=%s",
            investigation_id,
            status,
            total_cost,
            duration,
            root_cause[:100],
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

    # Seal the proposal into the ledger.
    store.append_ledger(
        investigation_id,
        "remediation.proposed",
        {"action_id": action_id, "kind": kind, "service": service, "reason": reason},
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
        from remediation import execute_action

        store.update_action(action_id, status="approved")
        try:
            result = await execute_action(kind, params)
            store.append_ledger(
                investigation_id,
                "remediation.executed",
                {
                    "action_id": action_id,
                    "kind": kind,
                    "service": service,
                    "approved_via": "auto-approve",
                },
            )
            return f"Remediation auto-approved and executed: {result}"
        except Exception as exc:
            return f"Remediation auto-approved but execution failed: {exc}"

    return (
        f"Remediation proposed: {kind}({service}) — {reason}. "
        f"Action ID: {action_id}. "
        f"Awaiting human approval via Slack or /approve/{action_id} endpoint."
    )

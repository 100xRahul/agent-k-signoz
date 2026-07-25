"""Independent auditor — a second LLM pass that screens a finished RCA for
groundedness before it is published.

The auditor gets its OWN fresh context (never the investigation's message
history) and only the two things it needs to judge: the RCA report and the raw
evidence the agent actually gathered. Its job is adversarial — flag every claim,
number, or deep link that is not backed by that evidence.

Pointing `AUDITOR_MODEL` (and optionally `AUDITOR_BASE_URL`/`AUDITOR_API_KEY`) at
a different model from the writer is what makes this check genuinely independent;
by default it reuses the main LLM endpoint.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from config import settings
from telemetry import audit_call_span, set_llm_usage

logger = logging.getLogger(__name__)

AUDITOR_SYSTEM_PROMPT = """You are an independent RCA auditor. A separate agent \
wrote the incident report below. You did NOT write it and you do not trust it.

Your only job is to check the GROUNDEDNESS of the report's ANALYTICAL claims — \
the substantive findings about the incident: the root cause, and every NUMBER \
(error rates, latencies, dollar amounts, user/tenant counts, error counts, \
version strings, and timestamps of OBSERVED events like deploys or error onset). \
Is each such claim supported by the EVIDENCE provided (the raw tool output the \
agent collected) or the INVESTIGATION CONTEXT block?

Rules:
- The INVESTIGATION CONTEXT block (the alert that triggered the run — its name, \
its fired/started time, labels, annotations, scenario) is GROUND TRUTH. Any \
claim that merely restates it (e.g. the alert name or the alert-fired timestamp) \
is grounded — do NOT flag it.
- IGNORE procedural and system boilerplate. These are NOT analytical claims and \
must NEVER be flagged: action IDs, approval endpoints/URLs (e.g. \
"/approve/..."), "Awaiting human approval", "Investigation finished", "Report \
saved", the agent's own Confidence label, the cost/token footer, "Investigation \
started: now", and generic status lines. Judge only substantive claims about the \
incident itself.
- A substantive claim is UNSUPPORTED only if its specific value does not appear \
in, and cannot be directly computed from, the evidence or context.
- Vague prose ("errors increased") without a backing number is a weak claim, not \
unsupported — only flag concrete numbers/entities stated as fact.
- Do NOT re-investigate or add new analysis. Judge only what is written.
- Be fair: if the evidence or context clearly backs a claim, it is grounded.

Respond with ONLY a JSON object, no prose, in exactly this shape:
{
  "grounded": true|false,
  "score": 0.0-1.0,          // fraction of SUBSTANTIVE claims that are supported
  "unsupported_claims": ["<claim>", ...],
  "notes": "<one-sentence overall assessment>"
}
"grounded" is true when there are no material unsupported SUBSTANTIVE claims \
(ignore boilerplate entirely when deciding)."""


@dataclass
class AuditResult:
    """Outcome of an independent audit pass."""

    outcome: str  # "grounded" | "ungrounded" | "error" | "skipped"
    grounded: bool | None = None
    score: float | None = None
    unsupported_claims: list[str] = field(default_factory=list)
    notes: str = ""
    error: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0

    def to_json(self) -> str:
        return json.dumps(
            {
                "outcome": self.outcome,
                "grounded": self.grounded,
                "score": self.score,
                "unsupported_claims": self.unsupported_claims,
                "notes": self.notes,
                "error": self.error,
            }
        )


def _extract_json(text: str) -> dict[str, Any]:
    """Pull the JSON object out of a model response (tolerates code fences/prose)."""
    text = text.strip()
    if text.startswith("```"):
        # strip ```json ... ``` fences
        text = text.split("```", 2)[1]
        text = text.removeprefix("json").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"no JSON object in auditor response: {text[:200]!r}")
    return json.loads(text[start : end + 1])


async def audit_rca(report_md: str, evidence: str) -> AuditResult:
    """Run the independent auditor over a finished RCA. Never raises — a failed
    audit returns outcome='error' so the caller can badge it (fail loud, not
    silently 'grounded')."""
    if not settings.auditor_enabled:
        return AuditResult(outcome="skipped")

    model = settings.effective_auditor_model
    client = AsyncOpenAI(
        base_url=settings.effective_auditor_base_url,
        api_key=settings.effective_auditor_api_key,
    )
    # Evidence can be large; cap it so the audit call stays cheap and bounded.
    # Keep BOTH ends: blast-radius / business-impact queries run last, so a
    # front-only truncation would hide exactly the numbers the auditor must
    # check. Keep a generous head and the full tail.
    cap = 120000
    if len(evidence) > cap:
        head = int(cap * 0.6)
        tail = cap - head
        evidence = (
            evidence[:head]
            + "\n\n... [middle of evidence truncated] ...\n\n"
            + evidence[-tail:]
        )

    user_msg = (
        f"## INCIDENT REPORT (under audit)\n\n{report_md}\n\n"
        f"## EVIDENCE (raw tool output the agent collected)\n\n{evidence}"
    )

    with audit_call_span(model) as span:
        try:
            audit_kwargs: dict[str, Any] = {
                "model": model,
                "messages": [
                    {"role": "system", "content": AUDITOR_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
            }
            # gpt-5* reject an explicit temperature; only send it when configured.
            if settings.llm_temperature is not None:
                audit_kwargs["temperature"] = settings.llm_temperature
            resp = await client.chat.completions.create(**audit_kwargs)
        except Exception as exc:
            logger.exception("Auditor LLM call failed")
            set_llm_usage(span, 0, 0, 0.0)
            span.set_attribute("agentk.audit.outcome", "error")
            return AuditResult(outcome="error", error=str(exc))

        usage = resp.usage
        tokens_in = usage.prompt_tokens if usage else 0
        tokens_out = usage.completion_tokens if usage else 0
        cost = settings.compute_cost(tokens_in, tokens_out)
        set_llm_usage(span, tokens_in, tokens_out, cost)

        content = resp.choices[0].message.content or ""
        try:
            parsed = _extract_json(content)
        except (ValueError, json.JSONDecodeError) as exc:
            logger.warning("Auditor returned unparseable output: %s", exc)
            span.set_attribute("agentk.audit.outcome", "error")
            return AuditResult(
                outcome="error",
                error=f"unparseable auditor response: {exc}",
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost,
            )

        grounded = bool(parsed.get("grounded", False))
        outcome = "grounded" if grounded else "ungrounded"
        score = parsed.get("score")
        try:
            score = float(score) if score is not None else None
        except (TypeError, ValueError):
            score = None
        claims = parsed.get("unsupported_claims") or []
        if not isinstance(claims, list):
            claims = [str(claims)]

        span.set_attribute("agentk.audit.outcome", outcome)
        span.set_attribute("agentk.audit.grounded", grounded)
        if score is not None:
            span.set_attribute("agentk.audit.score", score)

        return AuditResult(
            outcome=outcome,
            grounded=grounded,
            score=score,
            unsupported_claims=[str(c) for c in claims],
            notes=str(parsed.get("notes", "")),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
        )

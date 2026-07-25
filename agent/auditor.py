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

Your only job is to check GROUNDEDNESS: is every factual claim in the report — \
especially every NUMBER (error rates, latencies, dollar amounts, user/tenant \
counts, version strings) — supported by the EVIDENCE provided? The evidence is \
the raw tool output the writing agent actually collected.

Rules:
- A claim is UNSUPPORTED if its specific value does not appear in, and cannot be \
directly computed from, the evidence.
- Vague prose ("errors increased") without a backing number is a weak claim, not \
necessarily unsupported — only flag numbers/entities that are stated as fact.
- Do NOT re-investigate or add new analysis. Judge only what is written vs the \
evidence given.
- Be strict but fair: if the evidence clearly backs a claim, it is grounded.

Respond with ONLY a JSON object, no prose, in exactly this shape:
{
  "grounded": true|false,
  "score": 0.0-1.0,          // fraction of factual claims that are supported
  "unsupported_claims": ["<claim>", ...],
  "notes": "<one-sentence overall assessment>"
}
"grounded" is true only when there are no material unsupported claims."""


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
    if len(evidence) > 45000:
        evidence = evidence[:45000] + "\n... [evidence truncated]"

    user_msg = (
        f"## INCIDENT REPORT (under audit)\n\n{report_md}\n\n"
        f"## EVIDENCE (raw tool output the agent collected)\n\n{evidence}"
    )

    with audit_call_span(model) as span:
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": AUDITOR_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0,
            )
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

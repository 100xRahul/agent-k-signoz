"""Agent K configuration — all settings from environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All Agent K settings, loaded from environment variables."""

    # ── LLM Provider (OpenAI-compatible) ──────────────────────────
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = "sk-placeholder"
    openai_model: str = "gpt-4o-mini"

    # LLM cost accounting (per million tokens)
    llm_input_price_per_mtok: float = 0.0
    llm_output_price_per_mtok: float = 0.0

    # Sampling temperature. Some models (e.g. gpt-5*) only accept the default and
    # reject an explicit 0 — leave unset to OMIT temperature from the request;
    # set to 0 for deterministic runs on models that support it.
    llm_temperature: float | None = None

    # ── Independent auditor (groundedness gate) ───────────────────
    # A second LLM pass, with its own fresh context, screens each finished RCA
    # for groundedness before it is published. Enabled by default.
    auditor_enabled: bool = True
    # Empty → reuse `openai_model` on the same endpoint. Pointing this at a
    # DIFFERENT model (optionally via `auditor_base_url`/`auditor_api_key`) is
    # what makes the check genuinely independent of the writer.
    auditor_model: str = ""
    auditor_base_url: str = ""
    auditor_api_key: str = ""

    # ── SigNoz ────────────────────────────────────────────────────
    signoz_url: str = "http://localhost:8080"
    signoz_internal_url: str = "http://signoz:8080"
    signoz_api_key: str = ""
    mcp_url: str = "http://mcp-server:8000"

    # ── Slack ─────────────────────────────────────────────────────
    slack_webhook_url: str = ""

    # ── Agent K ───────────────────────────────────────────────────
    agent_public_url: str = "http://localhost:9000"
    approval_secret: str = "change-me-to-a-random-secret"
    auto_approve: bool = False
    max_iterations: int = 20
    max_cost_usd_per_investigation: float = 1.00
    # Alerts re-fire every eval interval; don't re-investigate the same alert
    # until this many minutes have passed since the last investigation started.
    investigation_cooldown_minutes: int = 15

    # ── Storage ───────────────────────────────────────────────────
    db_path: str = "/data/agentk.db"
    redis_url: str = "redis://sandbox-redis:6379"

    # ── Derived helpers ───────────────────────────────────────────

    def compute_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Compute USD cost from token counts using configured prices."""
        return (
            input_tokens * self.llm_input_price_per_mtok / 1_000_000
            + output_tokens * self.llm_output_price_per_mtok / 1_000_000
        )

    # Effective auditor endpoint — falls back to the main LLM config when the
    # auditor-specific override is unset (documented default, not silent).
    @property
    def effective_auditor_model(self) -> str:
        return self.auditor_model or self.openai_model

    @property
    def effective_auditor_base_url(self) -> str:
        return self.auditor_base_url or self.openai_base_url

    @property
    def effective_auditor_api_key(self) -> str:
        return self.auditor_api_key or self.openai_api_key

    model_config = {"env_prefix": "", "case_sensitive": False}


# Singleton — import this everywhere
settings = Settings()

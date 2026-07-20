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

    # ── SigNoz ────────────────────────────────────────────────────
    signoz_url: str = "http://localhost:8080"
    mcp_url: str = "http://mcp-server:8000"

    # ── Slack ─────────────────────────────────────────────────────
    slack_webhook_url: str = ""

    # ── Agent K ───────────────────────────────────────────────────
    agent_public_url: str = "http://localhost:9000"
    approval_secret: str = "change-me-to-a-random-secret"
    auto_approve: bool = False
    max_iterations: int = 20
    max_cost_usd_per_investigation: float = 1.00

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

    model_config = {"env_prefix": "", "case_sensitive": False}


# Singleton — import this everywhere
settings = Settings()

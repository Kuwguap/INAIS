"""All configuration. Nothing else in the codebase reads os.environ directly."""

from __future__ import annotations

import hashlib
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Core ---
    telegram_bot_token: str
    owner_telegram_id: int
    run_mode: str = "local"  # local | web

    # --- Brain ---
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    supabase_db_url: str = ""
    agent_model: str = "claude-sonnet-5"
    triage_model: str = "gpt-5-mini"
    reflection_model: str = "claude-haiku-4-5"
    embedding_model: str = "text-embedding-3-small"

    # --- Voice ---
    stt_model: str = "gpt-4o-mini-transcribe"
    tts_model: str = "gpt-4o-mini-tts"
    tts_voice: str = "nova"
    voice_replies: bool = True

    # --- Webhook (Render injects RENDER_EXTERNAL_URL and PORT) ---
    render_external_url: str = ""
    telegram_webhook_secret: str = ""
    webhook_secret_path: str = ""
    port: int = 10000

    # --- Gmail ---
    google_oauth_client_json: str = "google_oauth_client.json"
    gmail_poll_seconds: int = 60

    # --- Binance ---
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_symbols: str = "BTCUSDT,ETHUSDT"
    daily_summary_hour: int = 8

    # --- Planner (M8) ---
    morning_brief_hour: int = 7
    calendar_enabled: bool = False

    # --- Study (M9) ---
    study_nudge_hour: int = 17

    # --- GitHub (read-only) ---
    github_token: str = ""
    github_repos: str = ""          # comma-separated owner/repo for CI checks
    github_poll_minutes: int = 15

    # --- Sub-agent swarm (M10) ---
    subagent_model: str = "claude-haiku-4-5"
    max_parallel_subagents: int = 4

    # --- Growing brain (M11) ---
    learning_enabled: bool = False
    autonomy_interval_minutes: int = 30
    autonomy_idle_minutes: int = 45
    autonomy_topics_per_cycle: int = 2
    autonomy_daily_budget_usd: float = 1.0
    nn_enabled: bool = True
    nn_hidden_dim: int = 32
    nn_min_examples: int = 40
    tavily_api_key: str = ""
    brave_api_key: str = ""

    # --- Tracking (applications + expenses from email) ---
    default_currency: str = "USD"   # display currency for /spend totals

    # --- Ops ---
    timezone: str = "UTC"
    monthly_budget_usd: float = 50.0
    log_level: str = "INFO"

    # ---- derived ----
    @property
    def db_enabled(self) -> bool:
        return bool(self.supabase_db_url)

    @property
    def brain_enabled(self) -> bool:
        return bool(self.anthropic_api_key and self.openai_api_key)

    @property
    def binance_enabled(self) -> bool:
        return bool(self.binance_api_key and self.binance_api_secret)

    @property
    def github_enabled(self) -> bool:
        return bool(self.github_token)

    @property
    def github_repo_list(self) -> list[str]:
        return [r.strip() for r in self.github_repos.split(",") if r.strip()]

    @property
    def search_provider(self) -> str:
        """Which web-search backend the learning loop uses."""
        if self.tavily_api_key:
            return "tavily"
        if self.brave_api_key:
            return "brave"
        return "duckduckgo"  # no key needed, best-effort HTML scrape

    @property
    def symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.binance_symbols.split(",") if s.strip()]

    @property
    def webhook_path(self) -> str:
        """Random-ish URL path segment. Derived from the bot token if not set explicitly."""
        if self.webhook_secret_path:
            return self.webhook_secret_path
        return hashlib.sha256(f"wh:{self.telegram_bot_token}".encode()).hexdigest()[:32]


class DbSettings(BaseSettings):
    """Just the database URL.

    Maintenance scripts (migrations) legitimately run without a bot token or API keys, and
    Settings requires those. Env access still lives only in this module.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    supabase_db_url: str = ""


@lru_cache
def settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


@lru_cache
def db_settings() -> DbSettings:
    return DbSettings()

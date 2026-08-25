"""All configuration. Nothing else in the codebase reads os.environ directly."""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Telegram's secret_token alphabet, and what is safe in a URL path segment.
_TG_TOKEN_RE = re.compile(r"[^A-Za-z0-9_-]")
_URL_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


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
    brain_provider: str = "openai"  # auto | anthropic | openai — which brain runs the agent
    agent_model: str = "claude-sonnet-5"
    openai_agent_model: str = "gpt-5.6"   # used when the brain runs on OpenAI (alias → -sol)
    # GPT-5/o-series think before answering, and that thinking is most of the wall clock.
    # "low" keeps a chat assistant responsive; raise it only if answers are visibly shallow.
    openai_reasoning_effort: str = "low"   # minimal | low | medium | high
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

    # --- Reminders (alarm behaviour) ---
    reminder_burst: int = 4          # extra ping messages per firing (sent then deleted)
    reminder_nag_minutes: int = 3    # first re-ping delay; doubles each time
    reminder_max_nags: int = 3       # give up after this many re-pings

    # --- Planner (M8) ---
    morning_brief_hour: int = 7
    calendar_enabled: bool = False

    # --- Study (M9) ---
    study_nudge_hour: int = 17
    review_card_hour: int = 9       # daily spaced-repetition card

    # --- GitHub (read-only) ---
    github_token: str = ""
    github_repos: str = ""          # comma-separated owner/repo for CI checks
    github_poll_minutes: int = 15

    # --- Store (ogoffcl e-commerce) ---
    ogoffcl_base_url: str = ""      # website root, e.g. https://ogoffcl.store (bot calls {url}/api/bot)
    ogoffcl_api_key: str = ""       # shared secret: bot ↔ website, both directions

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
    serper_api_key: str = ""        # serper.dev — Google results, ~2,500 free/month
    tavily_api_key: str = ""
    brave_api_key: str = ""
    google_cse_api_key: str = ""    # Google Programmable Search backup, 100 free/day
    google_cse_id: str = ""

    # --- Personality & proactivity ---
    proactive_enabled: bool = False    # let it start conversations on its own
    proactive_max_per_day: int = 3     # hard cap; an assistant that pesters gets muted
    quiet_hours_start: int = 22        # never speaks unprompted between these hours
    quiet_hours_end: int = 8
    voice_notes_enabled: bool = True   # it may choose to reply by voice
    # Voice-persona knobs — how it carries itself. Free text so you can tune in the .env.
    persona_tone: str = "warm, direct"          # e.g. "playful", "formal", "deadpan"
    persona_brevity: str = "concise"            # concise | balanced | thorough
    persona_humour: bool = True
    # Below this predicted-engagement score an unprompted message is held back. Only applies
    # once the engagement network beats chance on held-out data.
    proactive_min_engagement: float = 0.3

    # --- Weekly review ---
    weekly_review_day: str = "sun"   # APScheduler day_of_week: mon..sun
    weekly_review_hour: int = 18

    # --- Reading digest ---
    reading_digest_day: str = "sun"  # weekly roundup of unread saved links
    reading_digest_hour: int = 20

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
    def agent_provider(self) -> str:
        """Which provider runs the agent loop.

        'auto' prefers Anthropic when its key is set, because Sonnet is the stronger
        tool-user — but plenty of accounts have an OpenAI key and no Claude access, so
        BRAIN_PROVIDER=openai moves everything (agent, sub-agents, reflection, grading)
        onto OpenAI without touching any other setting.
        """
        choice = (self.brain_provider or "auto").strip().lower()
        if choice in ("openai", "anthropic"):
            return choice
        return "anthropic" if self.anthropic_api_key else "openai"

    @property
    def resolved_agent_model(self) -> str:
        return (self.openai_agent_model if self.agent_provider == "openai"
                else self.agent_model)

    @property
    def resolved_subagent_model(self) -> str:
        """Sub-agents run cheap; on OpenAI that is the same tier as triage."""
        return (self.triage_model if self.agent_provider == "openai"
                else self.subagent_model)

    @property
    def brain_enabled(self) -> bool:
        if self.agent_provider == "openai":
            return bool(self.openai_api_key)
        return bool(self.anthropic_api_key and self.openai_api_key)

    @property
    def binance_enabled(self) -> bool:
        return bool(self.binance_api_key and self.binance_api_secret)

    @property
    def github_enabled(self) -> bool:
        return bool(self.github_token)

    @property
    def ogoffcl_enabled(self) -> bool:
        return bool(self.ogoffcl_base_url and self.ogoffcl_api_key)

    @property
    def github_repo_list(self) -> list[str]:
        return [r.strip() for r in self.github_repos.split(",") if r.strip()]

    @property
    def search_providers(self) -> list[str]:
        """Search backends in priority order; each is tried until one returns results.

        Serper first (largest free pool), Google CSE as the daily-capped backup, DuckDuckGo
        always last because it needs no key and best-effort beats nothing.
        """
        chain: list[str] = []
        if self.serper_api_key:
            chain.append("serper")
        if self.tavily_api_key:
            chain.append("tavily")
        if self.brave_api_key:
            chain.append("brave")
        if self.google_cse_api_key and self.google_cse_id:
            chain.append("google_cse")
        chain.append("duckduckgo")
        return chain

    @property
    def search_provider(self) -> str:
        """The primary backend (kept for /status and logs)."""
        return self.search_providers[0]

    @property
    def symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.binance_symbols.split(",") if s.strip()]

    @property
    def webhook_path(self) -> str:
        """URL path segment. Sanitised: a generated value may contain / + = which would
        silently break the route, and the derived fallback is hex so it is always safe."""
        cleaned = _URL_SAFE_RE.sub("", self.webhook_secret_path)[:64]
        if cleaned:
            return cleaned
        return hashlib.sha256(f"wh:{self.telegram_bot_token}".encode()).hexdigest()[:32]

    @property
    def webhook_secret(self) -> str:
        """Header token Telegram echoes back on every update.

        Telegram accepts ONLY A-Z a-z 0-9 _ - here and rejects setWebhook outright otherwise,
        so whatever the platform generated is filtered to that alphabet. Both setWebhook and
        the request check read this same property, so they cannot disagree. Falling back to a
        token-derived value keeps verification on even when the variable is unset.
        """
        cleaned = _TG_TOKEN_RE.sub("", self.telegram_webhook_secret)[:256]
        if cleaned:
            return cleaned
        return hashlib.sha256(f"secret:{self.telegram_bot_token}".encode()).hexdigest()


class ScriptSettings(BaseSettings):
    """What the one-off maintenance scripts need, and nothing else.

    Applying migrations and authorizing a Gmail account both run BEFORE the bot is
    configured — demanding a Telegram token to do either is pure friction. Env access still
    lives only in this module.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    supabase_db_url: str = ""
    google_oauth_client_json: str = "google_oauth_client.json"
    calendar_enabled: bool = False


@lru_cache
def settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


@lru_cache
def script_settings() -> ScriptSettings:
    return ScriptSettings()

# INAIS — Personal AI Assistant

Single-user assistant living in Telegram: hybrid Claude+OpenAI brain, Gmail email agent with
human-approval sends, read-only Binance finance agent, pgvector memory that learns the user,
voice notes in/out. One Python 3.12 asyncio process; state lives in Supabase Postgres.

## Commands

```bash
# setup (Windows: py -3.13 -m venv .venv && .venv\Scripts\activate)
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# run locally (long polling — no tunnel needed; set RUN_MODE=local in .env)
python -m inais.main

# apply DB migrations to Supabase (idempotent, ordered)
python scripts/apply_migrations.py

# authorize a Gmail account (run once per account, opens a browser)
python scripts/authorize_gmail.py you@gmail.com

# tests / lint
pytest
ruff check src tests
```

## Architecture map

- `src/inais/main.py` — entrypoint. `RUN_MODE=local` → aiogram long polling; `RUN_MODE=web` →
  aiohttp webhook server (ACKs 200 instantly, processes updates in background tasks).
- `src/inais/bot/` — aiogram routers (commands, chat, voice, approvals), inline keyboards,
  middleware (owner allowlist + update dedupe).
- `src/inais/orchestrator/` — `router.py` picks agent+model (rules → cheap OpenAI classifier);
  `loop.py` runs the Anthropic Messages API tool loop; `registry.py` maps agents → toolsets.
- `src/inais/agents/` — prompts + tool definitions per agent (email, finance, study).
- `src/inais/integrations/` — Gmail REST, Binance read-only client, STT/TTS (ffmpeg).
- `src/inais/memory/` — pgvector store, hybrid RRF retrieval, nightly reflection job.
- `src/inais/jobs/schedules.py` — all APScheduler jobs (Gmail poll, Binance snapshot,
  daily summary, nightly reflection, token health, budget alarm).
- `db/migrations/*.sql` — numbered, idempotent; tracked in `schema_migrations`.

## Conventions

- Python 3.12+, async everywhere; `asyncpg` with raw SQL (no ORM). Embeddings are passed as
  `'[0.1,0.2,...]'` strings cast with `::vector` (no numpy/pgvector client dep).
- aiogram 3.x style: `Router()` per module, registered in `bot/__init__.py`. Plain-text replies
  (no parse_mode) — never trust LLM output as Telegram markup.
- Every LLM call goes through `src/inais/llm.py` so usage/cost is recorded in `llm_usage`.
- Config comes only from `src/inais/config.py` (pydantic-settings, `.env`); never read
  `os.environ` elsewhere.
- Features degrade gracefully: missing optional env (Gmail/Binance/DB) logs a warning and
  disables that feature — the bot must still boot with just `TELEGRAM_BOT_TOKEN` + `OWNER_TELEGRAM_ID`.

## Security invariants (do not weaken)

1. Owner-only: middleware drops every update not from `OWNER_TELEGRAM_ID`.
2. No model ever gets a send-capable tool. Email sends happen ONLY in the human approval
   callback handler (`bot/routers/approvals.py`) after an inline-keyboard tap.
3. Binance key is read-only ("Enable Reading"); never add trade/withdraw permissions or endpoints.
4. Gmail scope is `gmail.modify` only (no delete scope).
5. Secrets never in the repo. Local: `.env` (gitignored). Render: Environment Group + Secret File.
6. Webhook: random path + `X-Telegram-Bot-Api-Secret-Token` verified on every request.
7. Idempotency: updates deduped by `update_id`; draft sends guarded by `status='pending'` check.

## Testing

Unit tests (pytest, no network): router rules, MIME building, callback-data encode/parse,
message splitting, importance prefilter. Integration = the milestone verify steps in
`README.md` against real Telegram/APIs. Never mock aiogram internals.

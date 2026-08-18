# INAIS — your personal AI assistant

A single-user assistant that lives in **Telegram** (text + voice) with a hybrid
**Claude + OpenAI** brain, an **email agent** (watches your Gmail accounts, flags important
mail, drafts replies **you approve before anything is sent**), a **finance agent**
(read-only Binance portfolio tracking + daily summaries), a **study/dev helper**, and
**long-term memory** on Supabase pgvector that consolidates what it learns about you every
night — including your writing style, learned from how you edit its drafts.

```
you (Telegram) ⇄ aiogram bot ⇄ router (cheap OpenAI) ⇄ Claude Sonnet tool loop
                                   agents: email · finance · study  + memory tools
                     APScheduler: gmail poll · binance snapshot · daily summary · nightly reflection
                     Supabase Postgres + pgvector: messages · facts · preferences · profile · drafts
```

Running cost: **$7/mo Render Starter + roughly $25–45/mo API** at ~200 messages/day.

---

## Setup — milestone by milestone

Each step is independently testable. Stop at any point and you still have a working system.

### M0 — Echo bot (10 min)
1. Telegram: talk to **@BotFather** → `/newbot` → copy the token.
2. Get your numeric user id from **@userinfobot**.
3. ```bash
   py -3.13 -m venv .venv        # or: python3.12 -m venv .venv
   .venv\Scripts\activate        # Windows (macOS/Linux: source .venv/bin/activate)
   pip install -e ".[dev]"
   copy .env.example .env        # then fill TELEGRAM_BOT_TOKEN + OWNER_TELEGRAM_ID
   python -m inais.main
   ```
4. **Verify:** message your bot → it answers. Any other Telegram account gets silence.

### M1 — Brain + database
1. Create an [Anthropic API key](https://platform.claude.com/) (needs billing) and have your
   OpenAI key ready → put both in `.env`.
2. Supabase → your project → Connect → copy the **session pooler** connection string
   (port 5432) into `SUPABASE_DB_URL`.
3. ```bash
   python scripts/apply_migrations.py
   python -m inais.main
   ```
4. **Verify:** hold a conversation; ask a hard multi-step question (routes to Claude) and
   say "hi" (answered by the cheap model — check the logs). Rows appear in `messages`.

### M2 — Memory
Nothing to configure (migrations already created it).
**Verify:** tell it *"remember that my thesis advisor is Dr. X"* → `/reflect` → `/reset` →
ask *"who is my advisor?"* — it answers from memory.

### M3 — Voice
Local dev needs `ffmpeg` on PATH (`choco install ffmpeg` / `brew install ffmpeg`). Docker image has it.
**Verify:** send a voice note → you get a transcript, a text answer, and a voice bubble back.

### M4 — Deploy to Render
1. Push this repo to GitHub. In Render: **New → Blueprint** → pick the repo (`render.yaml` drives it).
2. Fill the env vars it prompts for (everything marked `sync: false`); generate a long random
   `TELEGRAM_WEBHOOK_SECRET`.
3. **Verify:** turn your computer off, message the bot → it answers.
   `https://api.telegram.org/bot<TOKEN>/getWebhookInfo` shows your Render URL, no errors.

### M5 — Email agent (~20 min, one-time)
1. [Google Cloud Console](https://console.cloud.google.com/) → new project → enable **Gmail API**.
2. **OAuth consent screen** → External → fill the 3 required fields → **PUBLISH APP**
   ("In production"). Do **not** submit for verification — you're the only user. This step is
   what stops refresh tokens dying every 7 days.
3. **Credentials → Create credentials → OAuth client ID → Desktop app** → download JSON →
   save as `google_oauth_client.json` in the repo root (gitignored).
4. Per Gmail account: `python scripts/authorize_gmail.py you@gmail.com`
   (browser opens; the "Google hasn't verified this app" warning is expected —
   Advanced → Continue).
5. On Render: add the same JSON as a **Secret File** named `google_oauth_client.json`.
6. **Verify:** email yourself something urgent-sounding → Telegram pings you within ~1 min →
   tap **✍️ Draft reply** → tap **✅ Approve & send** → check the recipient's inbox.

### M6 — Finance agent
1. Binance → API Management → create key with **“Enable Reading” ONLY** — leave withdrawals
   and trading OFF, skip the IP whitelist. Put key+secret in `.env` / Render env.
2. Set `BINANCE_SYMBOLS` to the pairs you trade (for trade history).
3. **Verify:** `/finance` shows your real balances; the daily summary arrives at
   `DAILY_SUMMARY_HOUR`; a Binance login-alert email triggers an instant 🔐 notification.

### M7 — It learns
Already running: every night at 03:00 it distills conversations into facts, learns style rules
from your draft edits, and refreshes its profile of you.
**Verify:** edit a draft before approving (e.g. make it shorter) → after tonight's reflection
(or `/reflect`), the next draft matches your style. `/usage` shows month-to-date spend; you get
an alarm if it passes `MONTHLY_BUDGET_USD`.

---

## Commands

| Command | What it does |
|---|---|
| `/finance` | portfolio snapshot with 24h change |
| `/usage` | month-to-date AI spend by model |
| `/reflect` | run memory consolidation now |
| `/reset` | fresh conversation context |
| `/help` | overview |

## Security model

- **Owner-only**: every update from anyone but you is dropped.
- **Nothing sends without you**: the AI can only create drafts; the send button is yours.
- **Binance is read-only**; withdrawal permissions are never enabled.
- Gmail scope is `gmail.modify` (no delete). Secrets live in `.env` / Render, never in git.

## Development

```bash
pytest                 # unit tests
ruff check src tests   # lint
```

See [AGENTS.md](AGENTS.md) for conventions (Claude Code, Cursor and Codex read it too).

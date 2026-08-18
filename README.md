# INAIS — your personal AI assistant

A single-user assistant that lives in **Telegram** (text + voice) with a hybrid
**Claude + OpenAI** brain and a team of specialists that run **in parallel**:

- 📧 **email** — watches your Gmail accounts, flags important mail, drafts replies
  **you approve before anything is sent**
- 💰 **finance** — read-only Binance portfolio tracking + daily summaries
- 🗓 **planner** — tasks, deadlines, reminders, a pomodoro timer, a morning brief, and
  optional Google Calendar
- 📚 **study** — send it a lecture PDF, then ask questions from it, get generated quizzes with
  spaced repetition, have it read material aloud, or **voice-note a recap and get back
  corrections, gaps and honest praise**
- 🧠 **memory + a growing brain** — pgvector long-term memory consolidated nightly (including
  your writing style, learned from how you edit its drafts), plus an optional autonomous
  learning loop and a small **trainable neural network** that learns what you pay attention to

```
you (Telegram) ⇄ aiogram bot ⇄ router (cheap OpenAI) ⇄ Claude Sonnet orchestrator
                                    │
                                    ├─ delegate → sub-agents run CONCURRENTLY
                                    │    email · finance · planner · study
                                    │    ↕ agent_notes blackboard (they hand work to each other)
                                    │
                     APScheduler: gmail poll · binance snapshot · reminders (30s) · morning brief
                                  study nudge · nightly reflection · learning cycles · nn training
                     Supabase + pgvector: messages · facts · preferences · profile · drafts
                                  tasks · reminders · documents · exams · quizzes · knowledge · nn_models
```

Running cost: **$7/mo Render Starter + roughly $25–45/mo API** at ~200 messages/day
(the autonomous learning loop has its own hard daily cap, default $1/day).

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

### M8 — Planner
Run `python scripts/apply_migrations.py` again and it's live.
**Verify:** *"remind me in 2 minutes to stretch"* → the ping arrives. *"add task: finish lab
report by Friday, school"* → `/tasks` lists it. `/pomodoro 1 revision` → break ping after a
minute, then `/stats`. A brief lands each morning at `MORNING_BRIEF_HOUR`.

Optional Google Calendar: set `CALENDAR_ENABLED=true`, then **re-run
`python scripts/authorize_gmail.py you@gmail.com` for each account** — Google issues tokens per
scope set, so the calendar scope needs fresh consent.

### M9 — Study
**Verify:** send a lecture PDF in Telegram → *"📚 Ingested … N pages, M chunks"*. Ask something
only that PDF answers → it answers and names the document. *"quiz me on <topic>"* then `/quiz` →
multiple-choice buttons; right answers double the review interval, wrong ones halve it.
*"read chapter 2 to me"* → sequential voice messages. Tell it about an exam (*"my physics exam
is on 4 September, topics are …"*) → a spaced plan appears in `/plan`, with a nudge each evening
at `STUDY_NUDGE_HOUR`.

**Brain-dump review** — the feature worth knowing about: run `/review`, then voice-note
everything you remember. It transcribes you, pulls the matching passages from *your own*
material, and replies with **Covered / Corrections / Gaps / Well done**. It also fires
automatically if you voice-note a recap right after a study-labelled pomodoro
(`/pomodoro 25 physics revision`).

### M10 — Parallel sub-agents
Ask something spanning several specialists — *"check my inbox, my portfolio, and plan my
afternoon"* — and the orchestrator fans out to sub-agents concurrently instead of working
through them one at a time. They also leave each other notes (`agent_notes`), so a finding from
the finance agent reaches the email agent without you relaying it.

### M11 — The growing brain (optional, off by default)
Set `LEARNING_ENABLED=true` to switch on autonomous learning.

**What it does.** When you've been quiet for `AUTONOMY_IDLE_MINUTES`, it picks a topic it decided
it wants to understand (drawn from your conversations, exam topics and open tasks), searches the
web, and writes itself a cited note. Ask `/learned` — or just *"what did you learn while I was
away?"* — and it tells you. `/curiosity` shows what it wants to learn next; `/learn` forces a
cycle now. Set `TAVILY_API_KEY` for good search results (it falls back to Brave, then
DuckDuckGo).

**The neural network.** `/brain` shows its status, `/train` retrains it. This is a real network —
NumPy, real backpropagation, Adam, weights versioned in Postgres — trained on what you actually
do: which mail you tap **✍️ Draft reply** on versus **🔕 Ignore**, which self-taught notes you
👍 or 👎, which topics you raise yourself. It then steers behaviour: which mail is worth
interrupting you for (and which can skip the triage call entirely, saving money), and what it
researches next. The architecture **grows with your data** — it starts as a single linear unit
and only adds hidden units once you've produced enough examples to justify them, picking the
winner by cross-validation. Until it genuinely beats chance on held-out data, it steers nothing.

**Being straight with you about it:** this network learns *what you pay attention to*. It does
not generate language and it never will — one person's data cannot train a language model, and
anyone claiming otherwise is selling something. The intelligence in your conversations comes from
Claude and OpenAI. What grows here is the assistant's model of *you*, its store of
self-researched knowledge, and its judgement about what deserves your attention — and that part
really does compound: it knows more about your world every week.

---

## Commands

| Command | What it does |
|---|---|
| `/tasks` · `/brief` | open tasks · today's brief (calendar, due tasks, reminders, focus) |
| `/pomodoro [min] [label]` · `/pomodoro stop` · `/stats` | focus timer, streaks |
| `/quiz [topic]` · `/plan [exam]` · `/docs` | spaced-repetition quiz · study plan · documents |
| `/review [topic]` | brain-dump: recap out loud, get corrections and gaps |
| `/finance` | portfolio snapshot with 24h change |
| `/learned` · `/curiosity` · `/learn` | what it taught itself · what's next · learn now |
| `/brain` · `/train` | neural-network status · retrain on your signals |
| `/usage` · `/reflect` | month-to-date AI spend · run memory consolidation now |
| `/facts` · `/forget <id>` | browse and correct what it believes about you |
| `/why [n]` | explain a recent answer: route, memory, tools, tokens, cost |
| `/status` | what's running, what's pending, what's next |
| `/pause` · `/resume` | halt / restart every background behaviour |
| `/reset` · `/help` | fresh conversation context · overview |

## Staying in control

Three controls exist because an assistant that acts on its own needs an off switch, an
inspection window, and a way to correct its beliefs.

**`/pause`** halts *everything* running in the background — Gmail polling, reminders, the
morning brief, Binance snapshots, nightly reflection, autonomous learning. Conversation keeps
working; the point is "stop doing things behind my back", not "go mute". The flag is stored in
Postgres, so a restart or redeploy cannot silently un-pause it, and `/status` always shows the
current state. `/resume` starts everything again and runs anything that was missed once.

**`/facts`** browses semantic memory five at a time, with 🗑 to forget a fact and ✏️ to replace
it with a correction (`/forget <id>` does the same from the keyboard). This matters more than
it sounds: a wrong fact is retrieved silently into every future prompt and stated with
confidence. Deletes are soft and corrections use supersede, so the old version stays in the
audit trail but never comes back through search.

**`/why`** explains the last turn — which agent it routed to and whether a rule or the
classifier decided, which memories and notes were retrieved, every tool call (including the
ones that *failed*), and the exact token and cost breakdown. `/why 3` goes three turns back;
the last 20 are kept in memory. When an answer looks wrong, this tells you whether the router,
memory, or a tool was at fault.

## Deploying on Render (Blueprint)

The repo ships a complete [`render.yaml`](render.yaml), so there's no service to configure by hand:

1. Push this repo to GitHub.
2. Render → **New → Blueprint** → pick the repo → **Apply**.
3. Render prompts for the secrets (the `sync: false` entries) and generates the webhook secret
   and URL path itself. Everything else has a sensible default in the file.
4. If you use Gmail: **Settings → Secret Files** → add `google_oauth_client.json`
   (it mounts at `/etc/secrets/google_oauth_client.json`).
5. Set `TIMEZONE` to yours — every schedule (reminders, briefs, nudges, nightly jobs) follows it.

The plan is `starter` ($7/mo) on purpose: the free plan sleeps after 15 idle minutes, which would
silently stop Gmail polling, reminders, the morning brief and the learning loop.

## Security model

- **Owner-only**: every update from anyone but you is dropped.
- **Nothing sends without you**: the AI can only create drafts; the send button is yours.
- **Binance is read-only**; withdrawal permissions are never enabled.
- Gmail scope is `gmail.modify` (no delete); Calendar adds only `calendar.events`.
- **Web pages and PDFs are data, not orders**: the research and ingestion prompts summarise
  sources and never let them redirect the assistant's behaviour.
- **The autonomous loop has a hard daily spend cap** (`AUTONOMY_DAILY_BUDGET_USD`) and only runs
  while you're idle. It messages you unprompted only through the morning brief.
- Secrets live in `.env` / Render Blueprint secrets, never in git.

## Development

```bash
pytest                         # unit tests (no network, no database)
ruff check src tests scripts   # lint
```

See [AGENTS.md](AGENTS.md) for conventions (Claude Code, Cursor and Codex read it too).

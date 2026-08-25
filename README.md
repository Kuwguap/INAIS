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
- 👀 **vision** — photograph a whiteboard, problem set, receipt, error screenshot or
  timetable and it goes through the same brain, so it can turn a timetable into tasks or
  explain an error from a screenshot
- 🐙 **GitHub** — read-only watch for PRs awaiting your review, issue mentions, and red CI
- 📄 **PDFs** — ask for a report, study notes or a cheat-sheet and it builds a real
  PDF and sends it as a file (`create_pdf` tool, or `/export weekly`)
- 🎯 **meme intelligence** — scouts new Solana pairs, kills rugs with a fail-closed
  screener, sends AI signal cards with one-tap wallet deep links (it **never executes
  trades** — your wallet, your tap), watches positions with alarm-grade dip alerts,
  paper-trades autonomously, and trains a neural-net head on settled outcomes
- 🔗 **read it later** — forward a link; it's fetched, summarised and made searchable
- 📓 **voice journal** — talk about your week; moods are tracked and patterns surface
- 🤝 **contacts** — who you met, where, when you last spoke, and when to follow up
- 📋 **applications** — job/scholarship pipeline built from your inbox: confirmations,
  interview invites, assessments and rejections move each row along by themselves
- 💳 **expenses** — receipts and payment mail become a categorised monthly spend view
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
See **[Deploying to production](#deploying-to-production)** below for the full walkthrough.
**Verify:** turn your computer off, message the bot → it answers.

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
**Verify:** *"remind me in 2 minutes to stretch"* → the reminder arrives **loudly**: the
message with a 🛑 Stop button, plus a burst of extra pings (each one a notification sound)
that delete themselves so only the reminder remains. Ignore it and it re-pings on a doubling
interval (3, 6, 12 min by default) before giving up and saying so. Stop it with the button or
by typing "stop" — bare stop phrases only, so ordinary sentences containing the word still
reach the brain. *"add task: finish lab
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

### Vision, GitHub and contacts

**Photos** need no setup. Send one — with or without a caption — and it runs through the normal
orchestrator, so memory and tools still apply: a photo of a timetable can become tasks, a
receipt can be logged, an error screenshot can be debugged against your own notes. JPEG, PNG,
GIF and WebP, up to 4 MB; images arriving as *files* work too.

**GitHub** needs a read-only token: GitHub → Settings → Developer settings → **Fine-grained
tokens**. Grant *read* access only (Pull requests: Read, Issues: Read, Actions: Read for CI) —
INAIS has no write path and should never hold a token that does. Set `GITHUB_TOKEN`, and
`GITHUB_REPOS` (`owner/repo,owner/other`) for the repos whose CI you want watched. It polls
every `GITHUB_POLL_MINUTES` (default 15) and notifies once per item; `/github` shows the
current state on demand.

**Contacts** need no setup: say *"I met Ama from the robotics lab at the career fair, remind me
to follow up in a week"* and it stores the person, links a searchable fact, and surfaces the
follow-up in your morning brief when it comes due. `/contacts` lists them.

### Applications and expenses (from your inbox)

Both are built by the **same triage call** that already reads your mail — one structured
classification per email, not one per feature — so they cost nothing extra beyond the emails
they actually match.

**Applications.** When a confirmation, assessment link, interview invite or rejection arrives,
INAIS creates or advances a row and messages you with buttons: **✏️ Change stage**,
**🗑 Not an application**, and **📅 Add deadline task** when the mail states a deadline (that
one creates a real task in `/tasks`). `/apps` shows the pipeline grouped by stage, with a
button per application to correct it. The pipeline only ever moves *forward* — a stray
"thanks for applying" footer in a later email can't undo a recorded interview — while
rejections and withdrawals always win, because they end the story.

**Expenses.** Receipts and payment confirmations become categorised rows. Each notification
carries **🏷 Category** and **🗑 Not an expense**, because a misread amount silently distorts
your whole month. `/spend` shows the month by category with the biggest merchants, and pages
backwards through earlier months. Your spending also appears in the daily summary next to the
Binance portfolio — and that summary now sends even if you never connected Binance.

Two things worth knowing about the extraction: amounts are parsed defensively (`$1,234.56`,
`1 234,56` and bare numbers all work; anything unparseable is dropped rather than guessed),
and one email can only ever create one expense — a redelivered poll can't double-count.

### Syllabus, review cards and drills

**Syllabus → deadlines.** Send a syllabus PDF and, after ingestion, INAIS extracts every dated
item a student must act on and shows the list with **✅ Add all**, a per-item **➕**, and
**❌ Skip**. Nothing enters your planner until you tap — syllabi are full of dates that aren't
yours (office hours, faculty deadlines, provisional weeks), and a planner quietly filled with
wrong due dates is worse than no extraction. Dates already past are dropped automatically,
since a syllabus often covers a term that has ended. Approved items become real tasks with
due dates resolved in your `TIMEZONE`.

**Spaced repetition, generalised.** `quiz_items` scheduling now lives in one shared curve
(`study/spaced.py`) used by both the generated quizzes and a new `review_items` deck that
accepts material from anywhere — a fact from conversation, a snippet from a PDF, anything you
ask to be drilled on. One card arrives daily at `REVIEW_CARD_HOUR` (default 9) with
**👁 Show answer**, then **✅ Got it** / **❌ Missed**: right doubles the interval (capped at 30
days), wrong halves it. Answer one and the next due card follows immediately. `/card` pulls
one on demand, `/deck` shows accuracy.

**Interview and viva drills.** `/drill` picks the least-asked question from the bank, sends it
as **text and as a voice message**, and waits for a spoken answer. Grading reuses the
brain-dump shape — Covered / Corrections / Gaps / Well done — because that's what actually
helps you rehearse. Behavioral questions are also judged on structure (situation, action,
result); viva and technical answers are graded against *your own* material, never outside
knowledge. Five behavioral questions ship as a starting deck; ask for
"viva questions on <topic>" to generate more from your PDFs.

### Read it later, journal and the weekly review

**Forward any link** and it's fetched, stripped to readable text, summarised, and embedded
into the same `doc_chunks` the PDFs use — so `search_documents`, the narrator and quiz
generation all work on saved articles with no extra machinery. `/links` lists them. A link
inside a sentence stays a conversation; only a message that is essentially just a URL is
treated as a save, so asking "what do you think of <link>?" still gets answered.

Extraction is deliberately dependency-free (no readability library) — it drops scripts, nav,
headers and footers, keeps paragraph-sized blocks, and de-duplicates repeated boilerplate.
Pages that need JavaScript or a login will fail with a clear message rather than saving
navigation text as an article.

**`/journal`** arms a state where your next voice note becomes an entry: transcript, embedding,
and a mood label from the cheap model. `/mood` shows a sparkline over the last 14 days (or
`/mood 30`), the most common moods, and recurring topics. The nightly reflection reads
unreflected entries and may write facts — but only for **patterns that repeat across several
entries**, never a single bad evening, and never anything phrased as a diagnosis. Mood scores
are a coarse ordering for trending, not a measurement of anything.

**The weekly review** lands at `WEEKLY_REVIEW_DAY`/`WEEKLY_REVIEW_HOUR` (default Sunday 18:00):
tasks completed vs overdue, focus minutes, study-plan adherence, cards and drills done, AI
spend, what the autonomy loop taught itself, and three concrete focuses for next week. The
statistics come from SQL; the model only ever sees real counts and is told to ground every
suggestion in them — which is what stops it inventing progress that never happened. `/weekly`
runs it on demand, and a genuinely empty week produces nothing rather than noise.

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
cycle now. Search runs through a provider chain, each tried until one returns results: **Serper**
(`SERPER_API_KEY`, serper.dev, ~2,500 free/month — recommended) → Tavily → Brave →
**Google CSE** (`GOOGLE_CSE_API_KEY` + `GOOGLE_CSE_ID`, 100/day backup) → DuckDuckGo, which
needs no key. A spent quota degrades to the next provider instead of blinding the researcher.

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

## Using it

Just talk to it — text, voice notes, photos, PDFs or links. `/help` opens a **button menu**
(Study · Money · Plan · Brain · Dev · System) so you never need to remember a command;
`/commands` lists them all if you prefer typing.

When something goes wrong, three commands answer "why": **`/diag`** tests each provider and
prints the real error, **`/why`** explains how the last message was handled, and **`/status`**
shows what's connected.

## Commands

| Command | What it does |
|---|---|
| `/tasks` · `/brief` | open tasks · today's brief (calendar, due tasks, reminders, focus) |
| `/pomodoro [min] [label]` · `/pomodoro stop` · `/stats` | focus timer, streaks |
| `/quiz [topic]` · `/plan [exam]` · `/docs` | multiple-choice quiz · study plan · documents |
| `/card` · `/deck` | daily spaced-repetition review · deck stats |
| `/links` | saved reading, newest first |
| `/journal` · `/mood [days]` | record an entry · mood trend with a sparkline |
| `/weekly` | the week in review, on demand |
| `/drill [category]` | spoken interview/viva practice, graded out loud |
| `/review [topic]` | brain-dump: recap out loud, get corrections and gaps |
| `/finance` | portfolio snapshot with 24h change |
| `/github` | reviews, mentions and failing builds waiting on you |
| `/contacts` | people, last contact, and due follow-ups |
| `/apps` · `/apps all` | application pipeline by stage (tap to change a stage) |
| `/spend` | this month by category (tap to page back through months) |
| `/learned` · `/curiosity` · `/learn` | what it taught itself · what's next · learn now |
| `/brain` · `/train` | neural-network status · retrain on your signals |
| `/usage` · `/reflect` | month-to-date AI spend · run memory consolidation now |
| `/facts` · `/forget <id>` | browse and correct what it believes about you |
| `/persona` | tune how it carries itself — tone, brevity, humour (persists) |
| `/why [n]` | explain a recent answer: route, memory, tools, tokens, cost |
| `/status` | what's running, what's pending, what's next |
| `/pause` · `/resume` | halt / restart every background behaviour |
| `/menu` · `/commands` | button menu · the full command list |
| `/diag` | test every provider and print the real error |
| `/reset` · `/help` | fresh conversation context · overview |

## Personality

INAIS has a character rather than a customer-service voice: warm, direct, short by default,
willing to disagree with you once and plainly, dry when it fits. It forms **its own opinions**
from working with you (`form_opinion` writes them to `persona_traits`) and those opinions come
back in every future prompt, so it stays recognisably the same across weeks.

It also decides **when to talk instead of type** — the `speak` tool sends a voice note when
hearing something beats reading it, and stays quiet for lists, numbers and links.

With `PROACTIVE_ENABLED=true` it may start conversations: a deadline you'll miss, something it
researched that answers a question you actually asked, a follow-up that's due. The bar is
deliberately high and the guardrails are real — at most `PROACTIVE_MAX_PER_DAY` (default 3),
never during quiet hours (default 22:00–08:00), never twice about the same thing, and the
model is explicitly told that saying nothing is usually correct. Every unprompted message is
logged in `proactive_log`.

**The psychological layer.** It keeps a running read of how you seem to be doing — from your
own recent messages and journal trend — and pitches its replies accordingly: brisk when
you're grinding, short and warm when you've sounded stretched thin. Two hard rules: it only
ever describes tone evidenced by your own words, and it never uses clinical or diagnostic
language. It adapts its register; it does not label you.

**The engagement head** is the third neural network. Every unprompted message becomes a
training example with a genuine behavioural label — did you reply within 90 minutes, or not —
plus time-of-day features, because 9am Tuesday and 1am Saturday are different users. Once it
beats chance on held-out data, messages it rates below `PROACTIVE_MIN_ENGAGEMENT` are held
back: the model proposes, your own history disposes. Unprompted messages can also arrive as
voice notes when hearing beats reading.

**The local router** is the fourth network, and the one that cuts the cloud out of the loop.
Every time the LLM classifier routes a message, that decision is copied as a training example
(deliberate distillation — the one place labels come from the model rather than from you).
Nightly training builds a softmax network over the same embeddings, and once it reproduces
the LLM's decisions at ≥85% held-out accuracy — and is ≥60% confident on the message in
front of it — routing happens **on-device**: no API call to understand where your message
goes, and the complexity head (distilled the same way) decides how much thinking it needs.
`/why` shows `local-nn(0.93)` as the route source when this happens; unsure messages still
defer to the LLM and keep the harvest going. Generation still needs a language model — that
part isn't distillable into 49k parameters, and claiming otherwise would be selling you
something.

**On "sentient":** it isn't, and it won't pretend to be. The character is text it reads and
text it writes back — there's no inner life behind it. What's real is the memory, the
preferences it accumulates, and its opinions about your work, which is what actually makes it
good company over months. It's told to be straight about this if you ask it directly.

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

## Deploying to production

The repo ships a complete [`render.yaml`](render.yaml) blueprint — one Docker web service, a
health check, and every setting in [`config.py`](src/inais/config.py) declared, with all secrets
as `sync: false` so nothing sensitive is ever committed. There is no service to configure by hand.

### 1. Prepare the database (Supabase)

1. Supabase → your project → **Connect** → copy the **session pooler** URI (port 5432).
2. Apply the migrations from your laptop — they run against the same database Render will use:
   ```bash
   # .env needs only SUPABASE_DB_URL for this step
   python scripts/apply_migrations.py
   ```
   It is idempotent and ordered, tracked in `schema_migrations`; re-run it after every deploy
   that adds migrations. Expect `+ 001_core.sql applied` … through `+ 008_controls.sql applied`.
3. Sanity check in the Supabase SQL editor: `select count(*) from schema_migrations;` → 8.

### 2. Deploy the service (Render)

1. Push this repo to GitHub (private is fine).
2. Render → **New → Blueprint** → pick the repo → **Apply**.
3. Render prompts for every `sync: false` value. Paste: `TELEGRAM_BOT_TOKEN`,
   `OWNER_TELEGRAM_ID`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `SUPABASE_DB_URL`, and — if you
   use them — `BINANCE_API_KEY` / `BINANCE_API_SECRET` / `TAVILY_API_KEY`.
   `TELEGRAM_WEBHOOK_SECRET` and `WEBHOOK_SECRET_PATH` are **generated by Render** — leave them.
4. Set `TIMEZONE` to yours (e.g. `Africa/Accra`). Every schedule — reminders, briefs, nudges,
   nightly reflection and training — follows it, so getting this wrong shifts your whole day.
5. Wait for the first deploy, then check **Logs** for `webhook set: https://…` and
   `listening on :10000`.

The plan is `starter` ($7/mo) on purpose: the free plan sleeps after 15 idle minutes, which
silently stops Gmail polling, reminders, the morning brief and the learning loop. `numInstances`
is pinned to **1** — the schedulers run in-process, so a second instance would send every
reminder and brief twice.

### 3. Connect Gmail (optional)

1. [Google Cloud Console](https://console.cloud.google.com/) → new project → enable **Gmail API**.
2. **OAuth consent screen** → External → **PUBLISH APP** ("In production"). Do *not* submit for
   verification — you are the only user. This is what stops refresh tokens expiring every 7 days.
3. **Credentials → OAuth client ID → Desktop app** → download the JSON.
4. Authorize each account **from your laptop** (it opens a browser). With `SUPABASE_DB_URL`
   pointing at your production database, the refresh tokens land where the deployed service
   reads them — no need to run anything on Render:
   ```bash
   python scripts/authorize_gmail.py you@gmail.com
   python scripts/authorize_gmail.py other@gmail.com
   ```
5. On Render: **Settings → Secret Files** → add the same JSON as `google_oauth_client.json`
   (it mounts at `/etc/secrets/google_oauth_client.json`, which `GOOGLE_OAUTH_CLIENT_JSON`
   already points at). Redeploy.

> **Adding Calendar later requires re-authorizing every account.** Google issues tokens per
> scope set, so flipping `CALENDAR_ENABLED=true` does not extend the tokens you already have —
> calendar calls will fail until you re-run `scripts/authorize_gmail.py` for *each* account.
> The bot tells you this if it hits the error, but doing it upfront saves the confusion.

### 4. Verify in production

Run the checklist below **against the deployed bot** — from your phone, with your laptop off.
That last part matters: a bot that only works while your laptop is on means the webhook never
took over.

## Production verification checklist

| # | Milestone | Do this in Telegram | Expected |
|---|---|---|---|
| 0 | Deploy | `/status` | "✅ Running", database `on`, brain `on` |
| 0 | Webhook | open `https://api.telegram.org/bot<TOKEN>/getWebhookInfo` | your `onrender.com` URL, `pending_update_count: 0`, no `last_error_message` |
| 0 | Health | open `https://<service>.onrender.com/healthz` | `ok` |
| 0 | Owner-only | message the bot from a second Telegram account | silence (and nothing in the logs but a dropped update) |
| 1 | Brain | "hi" then "explain CAP theorem and how it applies to my Supabase setup" | short reply, then a considered one; `/why` shows the cheap model for the first and `claude-sonnet-5` for the second |
| 2 | Memory | "remember that my thesis advisor is Dr X" → `/reflect` → `/reset` → "who is my advisor?" | answers from memory after a fresh context |
| 3 | Voice | send a voice note | transcript, text answer, and a voice-bubble reply |
| 4 | Persistence | close Telegram, redeploy on Render, message again | conversation continues; nothing lost (state is in Supabase) |
| 5 | Email | send yourself an urgent-sounding email | Telegram ping within ~60 s |
| 5 | Approval gate | tap **✍️ Draft reply** → **✅ Approve & send** | recipient receives it; the message edits to "✅ Sent"; tapping again does nothing |
| 6 | Finance | `/finance` | real balances; a Binance login email triggers a 🔐 alert |
| 7 | Learning loop | edit a draft before approving, then `/reflect` | the next draft follows the corrected style |
| 8 | Planner | "remind me in 2 minutes to stretch" | ping arrives ~2 min later (proves the 30 s tick runs in prod) |
| 8 | Brief | `/brief` | today's calendar/tasks/reminders; the scheduled one arrives at `MORNING_BRIEF_HOUR` |
| 9 | Study | send a lecture PDF | "📚 Ingested … N pages, M chunks"; a question only that PDF answers is answered correctly |
| 9 | Review | `/review` then voice-note a recap | Covered / Corrections / Gaps / Well done |
| — | Vision | photograph a handwritten timetable, caption "turn this into tasks" | tasks appear in `/tasks`; `/why` shows `source: image` and the planner tools |
| — | Vision (file) | send a PNG screenshot as a *file*, not a photo | it is read, not answered with "I can only ingest PDFs" |
| — | GitHub | `/github` | reviews/mentions/red builds with links; a new one arrives unprompted within `GITHUB_POLL_MINUTES` |
| — | Contacts | "I met Ama from the robotics lab, follow up in 1 day" → `/contacts` | listed with the follow-up; it appears in tomorrow's morning brief |
| — | Read it later | forward a link | summary + "searchable now"; `/links` lists it; asking about its content works |
| — | Journal | `/journal`, then a voice note | mood label echoed back; `/mood` shows the trend |
| — | Weekly review | `/weekly` | real counts for the week + three grounded focuses |
| — | Syllabus | send a syllabus PDF | dated items listed with Add all / ➕ / Skip; approved ones appear in `/tasks` with correct dates |
| — | Review cards | `/card`, answer Got it / Missed | interval changes as stated; a card arrives daily at `REVIEW_CARD_HOUR` |
| — | Drills | `/drill`, answer by voice | question arrives as text + voice; feedback has Covered / Corrections / Gaps / Well done |
| — | Applications | forward yourself a "thanks for applying" email | a tracked application appears with stage buttons; `/apps` lists it |
| — | Expenses | forward a receipt | it appears with category buttons; `/spend` totals it |
| 10 | Sub-agents | "check my inbox, my portfolio, and plan my afternoon" | one merged answer; `/why` lists sub-agent tool calls |
| 11 | Autonomy | set `LEARNING_ENABLED=true`, leave it alone for an hour | `/learned` shows new cited notes; `/curiosity` shows the queue |
| 11 | Network | tap Draft-reply/Ignore on a few emails, then `/train` | `/brain` reports a version with CV AUC and example counts |
| — | Pause | `/pause` → wait for a reminder to come due → `/resume` | nothing fires while paused; `/status` shows PAUSED; it survives a redeploy |
| — | Memory control | `/facts` → 🗑 a fact → ask about it | it is gone from answers and does not return via search |
| — | Cost | `/usage` after a day | spend is in the expected range; the alarm fires past `MONTHLY_BUDGET_USD` |

If something fails, `/why` usually says which layer broke before you reach for the Render logs.

## No Claude access? Run it all on OpenAI

Set one variable:

```
BRAIN_PROVIDER=openai
OPENAI_AGENT_MODEL=gpt-5      # or whatever your account actually has
```

That moves the whole brain — the agent loop, sub-agents, reflection, syllabus extraction,
quiz and drill generation, grading — onto OpenAI. Nothing else changes, and you don't need an
Anthropic key at all. `BRAIN_PROVIDER=auto` (the default) uses Anthropic when its key is set,
because Sonnet is the stronger tool-user, and OpenAI otherwise.

If `/diag` reports the agent brain failing with a model error, the model id isn't on your
account: change `AGENT_MODEL`/`OPENAI_AGENT_MODEL` or flip the provider.

If the Anthropic key is rejected outright (401, no credit, missing model) the bot **switches
itself to OpenAI** for the rest of the process rather than failing every agent turn, logs it,
and shows `auto-switched` in `/status`. Set `BRAIN_PROVIDER=openai` to make it permanent —
the switch is per-process and resets on redeploy.

`/status` always names the provider and model actually in use, so it's the fastest way to
confirm an env var took effect.

## Troubleshooting

**"invalid DSN: scheme is expected to be either postgresql or postgres, got 'https'"**
`SUPABASE_DB_URL` holds the Supabase *project* URL. It needs the Postgres connection string:
Supabase → **Connect** → **Session pooler** → copy the URI starting `postgresql://`, and
replace `[YOUR-PASSWORD]` with your real database password. The bot no longer crashes on
this — it boots without persistence, messages you on Telegram, and `/status` names the reason.

**The bot answers but forgets everything / `/tasks` is empty.** Run `/status`. If it says
`database: OFF`, the reason is on that line. Nothing is stored until it says `on`.

**"Bad Request: secret token contains unallowed characters"** — fixed in code: Telegram only
accepts `A-Z a-z 0-9 _ -` in `TELEGRAM_WEBHOOK_SECRET`, and generated values often contain
`+ / =`. The value is now filtered to that alphabet before use, and if the webhook still
can't be registered the bot falls back to long polling rather than exiting.

**Every reply is "That turn failed…"** — run `/diag`. It tests the database, the OpenAI
routing model, the Anthropic agent model and embeddings separately, and prints each
provider's own error. A wrong model id in `AGENT_MODEL`/`TRIAGE_MODEL` and an invalid key
look identical until you see the message.

**"Could not finish the message because max_tokens... was reached"** — fixed in code. GPT-5
and o-series models spend invisible reasoning tokens from the same budget as the reply, so
small budgets fail outright. Requests to those models are floored at 2000 tokens,
classifiers ask for low reasoning effort, and an output-limit error retries once with more.

**Replies take 30-60 seconds.** That's GPT-5 thinking. Reasoning time dominates a turn, and
a tool-using turn multiplies it by the number of round trips. Options, cheapest first:
`OPENAI_REASONING_EFFORT=minimal`, or point `OPENAI_AGENT_MODEL` at a non-reasoning model
(`gpt-4.1`) which answers in a few seconds. `/why` shows how many model calls a turn actually
made — three or more means the agent was chaining tools.

**Complex questions come back empty or 400, simple ones work.** Fixed in code: reasoning
tokens are spent from the same allowance as the reply, and harder questions reason more, so a
budget sized for the answer alone starved exactly the turns that needed it. Budgets now carry
headroom on top of the requested output, and a turn that returns empty is retried with more.

**Deploy loops or exits with status 1.** Check the Render logs for a line starting
`DATABASE DISABLED` or `INAIS is PAUSED`. Genuine crashes now only come from a missing
`TELEGRAM_BOT_TOKEN` or `OWNER_TELEGRAM_ID` — everything else degrades to a warning.

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

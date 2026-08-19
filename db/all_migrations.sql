-- INAIS — complete schema (migrations 001-022)
-- Paste into the Supabase SQL editor and run once. Idempotent; safe to re-run.

create extension if not exists vector;

-- ==================== 001_core.sql ====================
-- Core: episodic messages, Telegram dedupe, LLM usage tracking.
create extension if not exists vector;

create table if not exists messages (
    id         bigserial primary key,
    chat_id    bigint      not null,
    role       text        not null check (role in ('user', 'assistant', 'reset')),
    content    text        not null,
    ts         timestamptz not null default now(),
    embedding  vector(1536),
    fts        tsvector generated always as (to_tsvector('english', left(content, 100000))) stored,
    source     text        not null default 'telegram'
);
create index if not exists messages_chat_id_idx on messages (chat_id, id desc);
create index if not exists messages_fts_idx on messages using gin (fts);
create index if not exists messages_embedding_idx on messages
    using hnsw (embedding vector_ip_ops);

create table if not exists processed_updates (
    update_id bigint primary key,
    ts        timestamptz not null default now()
);

create table if not exists llm_usage (
    id            bigserial primary key,
    provider      text        not null,
    model         text        not null,
    purpose       text        not null,
    input_tokens  integer     not null default 0,
    output_tokens integer     not null default 0,
    cost_usd      numeric     not null default 0,
    ts            timestamptz not null default now()
);
create index if not exists llm_usage_ts_idx on llm_usage (ts);

-- ==================== 002_memory.sql ====================
-- Semantic + procedural memory, and hybrid (RRF) search functions.

create table if not exists facts (
    id                bigserial primary key,
    category          text        not null default 'general',
    statement         text        not null,
    embedding         vector(1536),
    fts               tsvector generated always as (to_tsvector('english', statement)) stored,
    confidence        real        not null default 0.8,
    source_message_id bigint,
    valid_from        timestamptz not null default now(),
    superseded_by     bigint references facts (id),
    created_at        timestamptz not null default now()
);
create index if not exists facts_fts_idx on facts using gin (fts);
create index if not exists facts_embedding_idx on facts using hnsw (embedding vector_ip_ops);
create index if not exists facts_active_idx on facts (id) where superseded_by is null;

create table if not exists preferences (
    id         bigserial primary key,
    agent_name text        not null default 'all',
    rule       text        not null,
    source     text        not null default 'inferred'
        check (source in ('stated', 'inferred', 'edit_correction')),
    strength   real        not null default 0.7,
    updated_at timestamptz not null default now()
);

create table if not exists user_profile (
    id          integer primary key,
    doc         text        not null default '',
    rendered_at timestamptz not null default now()
);

create table if not exists conversation_summaries (
    id         bigserial primary key,
    session_id text,
    summary    text not null,
    embedding  vector(1536),
    day        date not null default current_date
);

-- Hybrid search: Reciprocal Rank Fusion of full-text + vector ranks
-- (pattern from the official Supabase hybrid-search guide).
create or replace function hybrid_search_facts(
    query_text text,
    query_embedding vector(1536),
    match_count int,
    full_text_weight float default 1,
    semantic_weight float default 1,
    rrf_k int default 50
) returns setof facts
language sql stable as $$
with full_text as (
    select id, row_number() over (
        order by ts_rank_cd(fts, websearch_to_tsquery('english', query_text)) desc) as rank_ix
    from facts
    where superseded_by is null
      and fts @@ websearch_to_tsquery('english', query_text)
    limit least(match_count, 30) * 2
),
semantic as (
    select id, row_number() over (order by embedding <#> query_embedding) as rank_ix
    from facts
    where superseded_by is null and embedding is not null
    limit least(match_count, 30) * 2
)
select f.*
from full_text
full outer join semantic on full_text.id = semantic.id
join facts f on coalesce(full_text.id, semantic.id) = f.id
order by
    coalesce(1.0 / (rrf_k + full_text.rank_ix), 0.0) * full_text_weight +
    coalesce(1.0 / (rrf_k + semantic.rank_ix), 0.0) * semantic_weight desc
limit least(match_count, 30)
$$;

create or replace function hybrid_search_messages(
    query_text text,
    query_embedding vector(1536),
    match_count int,
    full_text_weight float default 1,
    semantic_weight float default 1,
    rrf_k int default 50
) returns setof messages
language sql stable as $$
with full_text as (
    select id, row_number() over (
        order by ts_rank_cd(fts, websearch_to_tsquery('english', query_text)) desc) as rank_ix
    from messages
    where role in ('user', 'assistant')
      and fts @@ websearch_to_tsquery('english', query_text)
    limit least(match_count, 30) * 2
),
semantic as (
    select id, row_number() over (order by embedding <#> query_embedding) as rank_ix
    from messages
    where role in ('user', 'assistant') and embedding is not null
    limit least(match_count, 30) * 2
)
select m.*
from full_text
full outer join semantic on full_text.id = semantic.id
join messages m on coalesce(full_text.id, semantic.id) = m.id
order by
    coalesce(1.0 / (rrf_k + full_text.rank_ix), 0.0) * full_text_weight +
    coalesce(1.0 / (rrf_k + semantic.rank_ix), 0.0) * semantic_weight desc
limit least(match_count, 30)
$$;

-- ==================== 003_email.sql ====================
-- Email agent: connected accounts, triaged events, approval-gated drafts.

create table if not exists gmail_accounts (
    email           text primary key,
    refresh_token   text        not null,
    last_history_id text,
    status          text        not null default 'active'
        check (status in ('active', 'needs_reauth')),
    created_at      timestamptz not null default now()
);

create table if not exists email_events (
    id           bigserial primary key,
    account      text        not null references gmail_accounts (email),
    gmail_msg_id text        not null,
    thread_id    text        not null default '',
    from_        text        not null default '',
    subject      text        not null default '',
    snippet      text        not null default '',
    importance   text        not null default 'normal'
        check (importance in ('high', 'normal', 'low')),
    notified_at  timestamptz,
    ts           timestamptz not null default now(),
    unique (account, gmail_msg_id)
);

create table if not exists drafts (
    id                bigserial primary key,
    kind              text        not null default 'email' check (kind in ('email')),
    account           text        not null,
    gmail_draft_id    text        not null,
    thread_id         text        not null default '',
    to_addr           text        not null,
    subject           text        not null,
    body              text        not null,
    status            text        not null default 'pending'
        check (status in ('pending', 'sending', 'edited', 'rejected', 'sent')),
    user_edit         text,
    edit_processed_at timestamptz,
    created_at        timestamptz not null default now(),
    sent_at           timestamptz
);
create index if not exists drafts_status_idx on drafts (status);

-- ==================== 004_finance.sql ====================
-- Finance agent: hourly portfolio snapshots (trades table reserved for future syncing).

create table if not exists finance_snapshots (
    id         bigserial primary key,
    ts         timestamptz not null default now(),
    balances   jsonb       not null,
    total_usdt numeric     not null
);
create index if not exists finance_snapshots_ts_idx on finance_snapshots (ts desc);

create table if not exists trades (
    id       bigserial primary key,
    symbol   text    not null,
    trade_id bigint  not null,
    ts       timestamptz,
    side     text,
    qty      numeric,
    price    numeric,
    raw      jsonb,
    unique (symbol, trade_id)
);

-- ==================== 005_planner.sql ====================
-- Planner: tasks, reminders, pomodoro sessions (M8)

create table if not exists tasks (
    id           bigserial primary key,
    context      text        not null default 'personal'
        check (context in ('school', 'work', 'personal')),
    title        text        not null,
    notes        text,
    due          timestamptz,
    priority     int         not null default 3,   -- 1 = highest
    status       text        not null default 'open'
        check (status in ('open', 'done', 'dropped')),
    created_at   timestamptz not null default now(),
    completed_at timestamptz
);
create index if not exists tasks_open_due_idx on tasks (due nulls last) where status = 'open';

create table if not exists reminders (
    id             bigserial primary key,
    text           text        not null,
    fire_at        timestamptz not null,
    recurring_cron text,                            -- 5-field cron; null = one-shot
    fired          boolean     not null default false,
    created_at     timestamptz not null default now()
);
create index if not exists reminders_due_idx on reminders (fire_at) where fired = false;

create table if not exists pomodoro_sessions (
    id         bigserial primary key,
    started_at timestamptz not null default now(),
    minutes    int         not null default 25,
    completed  boolean     not null default false,
    label      text,
    ended_at   timestamptz
);
create index if not exists pomodoro_recent_idx on pomodoro_sessions (started_at desc);

-- ==================== 006_study.sql ====================
-- Study agent: documents + chunks, exams, study plans, quizzes, brain-dump reviews (M9)

create table if not exists documents (
    id          bigserial primary key,
    filename    text        not null,
    title       text        not null,
    uploaded_at timestamptz not null default now(),
    pages       int         not null default 0,
    exam_id     bigint
);

create table if not exists doc_chunks (
    id          bigserial primary key,
    document_id bigint  not null references documents (id) on delete cascade,
    chunk_index int     not null,
    content     text    not null,
    embedding   vector(1536),
    fts         tsvector generated always as (to_tsvector('english', content)) stored
);
create index if not exists doc_chunks_fts_idx on doc_chunks using gin (fts);
create index if not exists doc_chunks_embedding_idx on doc_chunks using hnsw (embedding vector_ip_ops);
create index if not exists doc_chunks_doc_idx on doc_chunks (document_id, chunk_index);

create table if not exists exams (
    id         bigserial primary key,
    name       text        not null,
    date       date        not null,
    topics     text[]      not null default '{}',
    created_at timestamptz not null default now()
);

create table if not exists study_plan (
    id      bigserial primary key,
    exam_id bigint  not null references exams (id) on delete cascade,
    day     date    not null,
    focus   text    not null,
    done    boolean not null default false
);
create index if not exists study_plan_day_idx on study_plan (day) where not done;

create table if not exists quiz_items (
    id            bigserial primary key,
    document_id   bigint references documents (id) on delete set null,
    exam_id       bigint references exams (id) on delete set null,
    topic         text,
    question      text not null,
    answer        text not null,
    choices       jsonb not null default '[]'::jsonb,
    times_asked   int  not null default 0,
    times_correct int  not null default 0,
    interval_days int  not null default 1,
    next_review   date not null default current_date,
    created_at    timestamptz not null default now()
);
create index if not exists quiz_due_idx on quiz_items (next_review);

create table if not exists study_reviews (
    id         bigserial primary key,
    exam_id    bigint references exams (id) on delete set null,
    topic      text,
    transcript text not null,
    feedback   text not null,
    score_note text,
    created_at timestamptz not null default now()
);

-- Hybrid RRF search over document chunks (mirrors hybrid_search_facts/messages).
create or replace function hybrid_search_doc_chunks(
    query_text text,
    query_embedding vector(1536),
    match_count int,
    full_text_weight float default 1,
    semantic_weight float default 1,
    rrf_k int default 50
) returns setof doc_chunks
language sql stable as $$
with full_text as (
    select id, row_number() over (
        order by ts_rank_cd(fts, websearch_to_tsquery('english', query_text)) desc) as rank_ix
    from doc_chunks
    where fts @@ websearch_to_tsquery('english', query_text)
    limit least(match_count, 30) * 2
),
semantic as (
    select id, row_number() over (order by embedding <#> query_embedding) as rank_ix
    from doc_chunks
    where embedding is not null
    limit least(match_count, 30) * 2
)
select c.*
from full_text
full outer join semantic on full_text.id = semantic.id
join doc_chunks c on coalesce(full_text.id, semantic.id) = c.id
order by
    coalesce(1.0 / (rrf_k + full_text.rank_ix), 0.0) * full_text_weight +
    coalesce(1.0 / (rrf_k + semantic.rank_ix), 0.0) * semantic_weight desc
limit least(match_count, 30)
$$;

-- ==================== 007_brain.sql ====================
-- Growing brain: self-acquired knowledge, curiosity queue, agent blackboard,
-- and the trainable neural network's weights + training examples (M10/M11).

create table if not exists knowledge (
    id          bigserial primary key,
    topic       text        not null,
    summary     text        not null,
    detail      text,
    sources     jsonb       not null default '[]'::jsonb,
    confidence  real        not null default 0.6,
    embedding   vector(1536),
    fts         tsvector generated always as (
        to_tsvector('english', topic || ' ' || summary || ' ' || coalesce(detail, ''))) stored,
    source_kind text        not null default 'web'
        check (source_kind in ('web', 'document', 'conversation', 'reflection')),
    learned_at  timestamptz not null default now(),
    surfaced_at timestamptz,
    engagement  int         not null default 0   -- +1 when the user engages, -1 when dismissed
);
create index if not exists knowledge_fts_idx on knowledge using gin (fts);
create index if not exists knowledge_embedding_idx on knowledge using hnsw (embedding vector_ip_ops);
create index if not exists knowledge_recent_idx on knowledge (learned_at desc);

-- What the assistant has decided (on its own) it wants to learn.
create table if not exists curiosity_queue (
    id           bigserial primary key,
    topic        text        not null,
    reason       text,
    priority     real        not null default 0.5,
    status       text        not null default 'queued'
        check (status in ('queued', 'researching', 'done', 'skipped')),
    created_at   timestamptz not null default now(),
    picked_at    timestamptz,
    knowledge_id bigint references knowledge (id) on delete set null
);
create unique index if not exists curiosity_topic_uidx on curiosity_queue (lower(topic));
create index if not exists curiosity_ready_idx on curiosity_queue (priority desc) where status = 'queued';

-- Blackboard: how sub-agents hand findings to each other with no user in the loop.
create table if not exists agent_notes (
    id         bigserial primary key,
    from_agent text        not null,
    to_agent   text        not null default 'all',
    topic      text        not null,
    content    text        not null,
    ts         timestamptz not null default now(),
    consumed   boolean     not null default false
);
create index if not exists agent_notes_inbox_idx on agent_notes (to_agent, consumed, id desc);

-- Neural network weights, versioned. Only one row per name is active.
create table if not exists nn_models (
    id         bigserial primary key,
    name       text        not null,
    version    int         not null,
    input_dim  int         not null,
    hidden_dim int         not null,
    weights    bytea       not null,
    examples   int         not null default 0,
    metrics    jsonb       not null default '{}'::jsonb,
    trained_at timestamptz not null default now(),
    active     boolean     not null default false
);
create unique index if not exists nn_models_name_version_uidx on nn_models (name, version);
create index if not exists nn_models_active_idx on nn_models (name) where active;

-- Supervised training data harvested from real behaviour (taps, questions, engagement).
create table if not exists nn_examples (
    id         bigserial primary key,
    model_name text        not null,
    embedding  vector(1536) not null,
    label      real        not null,           -- 1.0 = positive, 0.0 = negative
    weight     real        not null default 1.0,
    note       text,
    created_at timestamptz not null default now()
);
create index if not exists nn_examples_model_idx on nn_examples (model_name, id desc);

create or replace function hybrid_search_knowledge(
    query_text text,
    query_embedding vector(1536),
    match_count int,
    full_text_weight float default 1,
    semantic_weight float default 1,
    rrf_k int default 50
) returns setof knowledge
language sql stable as $$
with full_text as (
    select id, row_number() over (
        order by ts_rank_cd(fts, websearch_to_tsquery('english', query_text)) desc) as rank_ix
    from knowledge
    where fts @@ websearch_to_tsquery('english', query_text)
    limit least(match_count, 30) * 2
),
semantic as (
    select id, row_number() over (order by embedding <#> query_embedding) as rank_ix
    from knowledge
    where embedding is not null
    limit least(match_count, 30) * 2
)
select k.*
from full_text
full outer join semantic on full_text.id = semantic.id
join knowledge k on coalesce(full_text.id, semantic.id) = k.id
order by
    coalesce(1.0 / (rrf_k + full_text.rank_ix), 0.0) * full_text_weight +
    coalesce(1.0 / (rrf_k + semantic.rank_ix), 0.0) * semantic_weight desc
limit least(match_count, 30)
$$;

-- ==================== 008_controls.sql ====================
-- Safety controls: a persisted pause switch and soft-deletable facts.

-- Runtime switches that must survive restarts (currently: 'paused').
create table if not exists runtime_flags (
    key        text primary key,
    enabled    boolean     not null default false,
    note       text,
    updated_at timestamptz not null default now()
);

-- A wrong fact silently poisons every future answer, so facts must be removable.
-- Soft delete keeps the audit trail while taking the row out of every retrieval path.
alter table facts add column if not exists deleted_at timestamptz;

drop index if exists facts_active_idx;
create index if not exists facts_live_idx on facts (id)
    where superseded_by is null and deleted_at is null;

-- Replaces the 002 definition: forgotten facts must never come back through search.
create or replace function hybrid_search_facts(
    query_text text,
    query_embedding vector(1536),
    match_count int,
    full_text_weight float default 1,
    semantic_weight float default 1,
    rrf_k int default 50
) returns setof facts
language sql stable as $$
with full_text as (
    select id, row_number() over (
        order by ts_rank_cd(fts, websearch_to_tsquery('english', query_text)) desc) as rank_ix
    from facts
    where superseded_by is null
      and deleted_at is null
      and fts @@ websearch_to_tsquery('english', query_text)
    limit least(match_count, 30) * 2
),
semantic as (
    select id, row_number() over (order by embedding <#> query_embedding) as rank_ix
    from facts
    where superseded_by is null and deleted_at is null and embedding is not null
    limit least(match_count, 30) * 2
)
select f.*
from full_text
full outer join semantic on full_text.id = semantic.id
join facts f on coalesce(full_text.id, semantic.id) = f.id
order by
    coalesce(1.0 / (rrf_k + full_text.rank_ix), 0.0) * full_text_weight +
    coalesce(1.0 / (rrf_k + semantic.rank_ix), 0.0) * semantic_weight desc
limit least(match_count, 30)
$$;

-- ==================== 009_github.sql ====================
-- GitHub watcher: one row per thing we've already told the user about, so a 15-minute
-- poll never re-notifies the same review request, mention or failed run.

create table if not exists github_events (
    id          bigserial primary key,
    kind        text        not null
        check (kind in ('review_request', 'mention', 'ci_failure')),
    event_key   text        not null unique,   -- stable per notifiable thing
    repo        text,
    title       text,
    url         text,
    notified_at timestamptz not null default now()
);
create index if not exists github_events_recent_idx on github_events (notified_at desc);

-- ==================== 010_contacts.sql ====================
-- People the user knows. Linked to a fact so contacts also surface through semantic search
-- ("who was the guy from the robotics lab?") rather than only exact-name lookup.

create table if not exists contacts (
    id           bigserial primary key,
    name         text        not null,
    org          text,
    how_met      text,
    last_contact timestamptz,
    follow_up_at date,
    notes        text,
    fact_id      bigint references facts (id) on delete set null,
    created_at   timestamptz not null default now()
);
create unique index if not exists contacts_name_uidx on contacts (lower(name));
create index if not exists contacts_followup_idx on contacts (follow_up_at)
    where follow_up_at is not null;

-- ==================== 011_applications.sql ====================
-- Job/scholarship application pipeline, driven by what lands in the inbox.

create table if not exists applications (
    id              bigserial primary key,
    org             text        not null,
    role            text,
    kind            text        not null default 'job'
        check (kind in ('job', 'internship', 'scholarship', 'grant', 'program', 'other')),
    status          text        not null default 'applied'
        check (status in ('applied', 'assessment', 'interview', 'offer', 'rejected', 'withdrawn')),
    applied_at      timestamptz not null default now(),
    deadline        timestamptz,
    source_email_id bigint references email_events (id) on delete set null,
    task_id         bigint references tasks (id) on delete set null,
    notes           text,
    updated_at      timestamptz not null default now()
);

-- One row per org+role, so a later rejection or interview mail updates the existing
-- application instead of creating a duplicate.
create unique index if not exists applications_org_role_uidx
    on applications (lower(org), lower(coalesce(role, '')));
create index if not exists applications_status_idx on applications (status, updated_at desc);

-- ==================== 012_expenses.sql ====================
-- Receipts and payment notifications extracted from email.

create table if not exists expenses (
    id              bigserial primary key,
    merchant        text          not null,
    amount          numeric(14, 2) not null,
    currency        text          not null default 'USD',
    category        text          not null default 'other'
        check (category in ('food', 'transport', 'subscription', 'shopping',
                            'bills', 'health', 'education', 'other')),
    occurred_at     timestamptz   not null default now(),
    source_email_id bigint references email_events (id) on delete set null,
    note            text,
    created_at      timestamptz   not null default now()
);
create index if not exists expenses_occurred_idx on expenses (occurred_at desc);

-- One expense per source email: a redelivered poll must not double-count spending.
create unique index if not exists expenses_source_uidx on expenses (source_email_id)
    where source_email_id is not null;

-- ==================== 013_syllabus.sql ====================
-- Dated items extracted from an ingested syllabus, held for approval before they
-- become real tasks. Nothing here reaches the planner without the user tapping.

create table if not exists syllabus_items (
    id          bigserial primary key,
    document_id bigint      not null references documents (id) on delete cascade,
    title       text        not null,
    kind        text        not null default 'assignment'
        check (kind in ('assignment', 'exam', 'reading', 'project', 'other')),
    due_at      timestamptz,
    detail      text,
    status      text        not null default 'pending'
        check (status in ('pending', 'approved', 'rejected')),
    task_id     bigint references tasks (id) on delete set null,
    created_at  timestamptz not null default now()
);
create index if not exists syllabus_pending_idx on syllabus_items (document_id)
    where status = 'pending';

-- ==================== 014_review_items.sql ====================
-- Spaced repetition over anything worth remembering, not just generated quizzes:
-- facts from conversation, snippets from PDFs, definitions the user asked about.

create table if not exists review_items (
    id            bigserial primary key,
    front         text        not null,
    back          text        not null,
    source_kind   text        not null default 'manual'
        check (source_kind in ('manual', 'conversation', 'document', 'fact', 'quiz')),
    source_id     bigint,
    topic         text,
    interval_days int         not null default 1,
    next_review   date        not null default current_date,
    times_asked   int         not null default 0,
    times_correct int         not null default 0,
    last_reviewed timestamptz,
    created_at    timestamptz not null default now()
);
create index if not exists review_due_idx on review_items (next_review);
create unique index if not exists review_front_uidx on review_items (lower(front));

-- ==================== 015_drills.sql ====================
-- Interview and viva practice: a question bank plus what the user actually answered.

create table if not exists drill_questions (
    id          bigserial primary key,
    category    text        not null default 'behavioral'
        check (category in ('behavioral', 'technical', 'viva')),
    exam_id     bigint references exams (id) on delete cascade,
    question    text        not null,
    guidance    text,                      -- what a strong answer covers; grading rubric
    times_asked int         not null default 0,
    last_asked  timestamptz,
    created_at  timestamptz not null default now()
);
create index if not exists drill_pick_idx on drill_questions (category, times_asked);
create unique index if not exists drill_question_uidx on drill_questions (lower(question));

create table if not exists drill_answers (
    id          bigserial primary key,
    question_id bigint      not null references drill_questions (id) on delete cascade,
    transcript  text        not null,
    feedback    text        not null,
    created_at  timestamptz not null default now()
);

-- ==================== 016_readlater.sql ====================
-- Read-it-later: saved web pages live alongside PDFs in documents/doc_chunks, so everything
-- the user has given the assistant is searchable through one path.

alter table documents add column if not exists source_url text;
alter table documents add column if not exists kind text not null default 'pdf';
alter table documents add column if not exists summary text;

do $$
begin
    if not exists (select 1 from pg_constraint where conname = 'documents_kind_check') then
        alter table documents add constraint documents_kind_check
            check (kind in ('pdf', 'link', 'note'));
    end if;
end $$;

-- Saving the same URL twice should update, not duplicate.
create unique index if not exists documents_url_uidx on documents (source_url)
    where source_url is not null;
create index if not exists documents_kind_idx on documents (kind, uploaded_at desc);

-- ==================== 017_journal.sql ====================
-- Voice journal. Entries are searchable (fts + embedding) so the nightly reflection can look
-- for patterns across weeks rather than reacting to a single bad day.

create table if not exists journal_entries (
    id         bigserial primary key,
    transcript text        not null,
    embedding  vector(1536),
    fts        tsvector generated always as (to_tsvector('english', transcript)) stored,
    mood       text        not null default 'okay'
        check (mood in ('great', 'good', 'okay', 'tired', 'stressed',
                        'anxious', 'frustrated', 'low', 'sad')),
    mood_score real        not null default 0,
    topics     text[]      not null default '{}',
    source     text        not null default 'voice' check (source in ('voice', 'text')),
    reflected  boolean     not null default false,
    created_at timestamptz not null default now()
);
create index if not exists journal_recent_idx on journal_entries (created_at desc);
create index if not exists journal_fts_idx on journal_entries using gin (fts);
create index if not exists journal_embedding_idx on journal_entries using hnsw (embedding vector_ip_ops);

-- ==================== 018_persona.sql ====================
-- The assistant's own character: what it has come to like, dislike and think, and a record
-- of every time it spoke without being spoken to (so it can be rate-limited and audited).

create table if not exists persona_traits (
    id         bigserial primary key,
    kind       text        not null default 'opinion'
        check (kind in ('like', 'dislike', 'opinion', 'habit', 'curiosity')),
    statement  text        not null,
    reason     text,
    strength   real        not null default 0.6,
    formed_at  timestamptz not null default now()
);
create unique index if not exists persona_traits_uidx on persona_traits (lower(statement));

create table if not exists proactive_log (
    id      bigserial primary key,
    kind    text        not null,
    content text        not null,
    sent_at timestamptz not null default now()
);
create index if not exists proactive_recent_idx on proactive_log (sent_at desc);

-- ==================== 019_engagement.sql ====================
-- The engagement head: learning WHEN speaking first actually lands.
--
-- proactive_log grows outcome columns so every unprompted message becomes a training
-- example with a genuine behavioural label: did the user reply within the window, or not?
-- nn_examples grows optional context features (time-of-day, weekday) because "will they
-- engage" depends on the clock as much as the content.

alter table proactive_log add column if not exists medium text not null default 'text'
    check (medium in ('text', 'voice'));
alter table proactive_log add column if not exists replied boolean;
alter table proactive_log add column if not exists reply_latency_s int;
alter table proactive_log add column if not exists harvested boolean not null default false;
create index if not exists proactive_unharvested_idx on proactive_log (sent_at)
    where not harvested;

alter table nn_examples add column if not exists context_features real[] not null default '{}';

-- ==================== 020_alarm.sql ====================
-- Alarm-grade reminders: a reminder is not delivered until the user says so.
-- fired = the first send happened; acknowledged = the user pressed Stop (or typed it).
-- Existing rows default to acknowledged so history does not start nagging on deploy.

alter table reminders add column if not exists acknowledged boolean not null default true;
alter table reminders add column if not exists nag_count int not null default 0;
alter table reminders add column if not exists nag_at timestamptz;
alter table reminders add column if not exists message_id bigint;

create index if not exists reminders_nag_idx on reminders (nag_at)
    where not acknowledged;

-- ==================== 021_engagement_backfill.sql ====================
-- Rows logged before reply-tracking existed have replied = null because nothing ever
-- watched for their replies — harvesting them as "no reply" would fabricate negative
-- engagement examples out of ignorance. Mark history as already harvested; only messages
-- sent under tracking become training data.

update proactive_log set harvested = true where replied is null and not harvested;

-- ==================== 022_reminder_lifecycle.sql ====================
-- last_fired_at: when the alarm actually rang. fire_at cannot serve that role because a
-- recurring reminder's fire_at is re-armed to the NEXT occurrence while it is still ringing,
-- which made typed-stop pick the wrong reminder to silence.

alter table reminders add column if not exists last_fired_at timestamptz;

-- ==================== bookkeeping ====================
create table if not exists schema_migrations (
    filename   text primary key,
    applied_at timestamptz not null default now()
);
insert into schema_migrations (filename) values
    ('001_core.sql'),
    ('002_memory.sql'),
    ('003_email.sql'),
    ('004_finance.sql'),
    ('005_planner.sql'),
    ('006_study.sql'),
    ('007_brain.sql'),
    ('008_controls.sql'),
    ('009_github.sql'),
    ('010_contacts.sql'),
    ('011_applications.sql'),
    ('012_expenses.sql'),
    ('013_syllabus.sql'),
    ('014_review_items.sql'),
    ('015_drills.sql'),
    ('016_readlater.sql'),
    ('017_journal.sql'),
    ('018_persona.sql'),
    ('019_engagement.sql'),
    ('020_alarm.sql'),
    ('021_engagement_backfill.sql'),
    ('022_reminder_lifecycle.sql')
on conflict do nothing;

select count(*) as tables_created from information_schema.tables where table_schema = 'public';
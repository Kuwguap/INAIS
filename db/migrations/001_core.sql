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

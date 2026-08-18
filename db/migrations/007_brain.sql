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

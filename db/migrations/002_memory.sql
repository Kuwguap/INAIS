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

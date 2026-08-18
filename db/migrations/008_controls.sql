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

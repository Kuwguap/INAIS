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

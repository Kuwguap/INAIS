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

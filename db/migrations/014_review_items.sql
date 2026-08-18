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

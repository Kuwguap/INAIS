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

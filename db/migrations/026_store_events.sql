-- Store push idempotency: one row per store event we've already notified the owner about,
-- so a re-delivered order.paid / waitlist.joined push (retries, double-fires) notifies once.

create table if not exists store_events (
    id          bigserial primary key,
    event_key   text        not null unique,   -- "{event}:{id}", e.g. order.paid:<uuid>
    kind        text,
    summary     text,
    created_at  timestamptz not null default now()
);
create index if not exists store_events_recent_idx on store_events (created_at desc);

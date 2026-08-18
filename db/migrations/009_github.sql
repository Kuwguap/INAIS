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

-- Email agent: connected accounts, triaged events, approval-gated drafts.

create table if not exists gmail_accounts (
    email           text primary key,
    refresh_token   text        not null,
    last_history_id text,
    status          text        not null default 'active'
        check (status in ('active', 'needs_reauth')),
    created_at      timestamptz not null default now()
);

create table if not exists email_events (
    id           bigserial primary key,
    account      text        not null references gmail_accounts (email),
    gmail_msg_id text        not null,
    thread_id    text        not null default '',
    from_        text        not null default '',
    subject      text        not null default '',
    snippet      text        not null default '',
    importance   text        not null default 'normal'
        check (importance in ('high', 'normal', 'low')),
    notified_at  timestamptz,
    ts           timestamptz not null default now(),
    unique (account, gmail_msg_id)
);

create table if not exists drafts (
    id                bigserial primary key,
    kind              text        not null default 'email' check (kind in ('email')),
    account           text        not null,
    gmail_draft_id    text        not null,
    thread_id         text        not null default '',
    to_addr           text        not null,
    subject           text        not null,
    body              text        not null,
    status            text        not null default 'pending'
        check (status in ('pending', 'sending', 'edited', 'rejected', 'sent')),
    user_edit         text,
    edit_processed_at timestamptz,
    created_at        timestamptz not null default now(),
    sent_at           timestamptz
);
create index if not exists drafts_status_idx on drafts (status);

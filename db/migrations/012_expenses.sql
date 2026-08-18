-- Receipts and payment notifications extracted from email.

create table if not exists expenses (
    id              bigserial primary key,
    merchant        text          not null,
    amount          numeric(14, 2) not null,
    currency        text          not null default 'USD',
    category        text          not null default 'other'
        check (category in ('food', 'transport', 'subscription', 'shopping',
                            'bills', 'health', 'education', 'other')),
    occurred_at     timestamptz   not null default now(),
    source_email_id bigint references email_events (id) on delete set null,
    note            text,
    created_at      timestamptz   not null default now()
);
create index if not exists expenses_occurred_idx on expenses (occurred_at desc);

-- One expense per source email: a redelivered poll must not double-count spending.
create unique index if not exists expenses_source_uidx on expenses (source_email_id)
    where source_email_id is not null;

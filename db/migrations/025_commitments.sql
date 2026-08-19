-- Commitments: things the user said they'd do, captured by the note_commitment tool so the
-- assistant can follow through and check in. Mirrors the contacts.follow_up_at pattern —
-- nullable due date + partial index over the open ones.

create table if not exists commitments (
    id         bigserial primary key,
    text       text not null,
    created_at timestamptz not null default now(),
    due_at     date,
    done       boolean not null default false,
    source_msg text
);

create index if not exists commitments_due_idx on commitments (due_at) where not done;

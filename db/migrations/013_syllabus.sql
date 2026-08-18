-- Dated items extracted from an ingested syllabus, held for approval before they
-- become real tasks. Nothing here reaches the planner without the user tapping.

create table if not exists syllabus_items (
    id          bigserial primary key,
    document_id bigint      not null references documents (id) on delete cascade,
    title       text        not null,
    kind        text        not null default 'assignment'
        check (kind in ('assignment', 'exam', 'reading', 'project', 'other')),
    due_at      timestamptz,
    detail      text,
    status      text        not null default 'pending'
        check (status in ('pending', 'approved', 'rejected')),
    task_id     bigint references tasks (id) on delete set null,
    created_at  timestamptz not null default now()
);
create index if not exists syllabus_pending_idx on syllabus_items (document_id)
    where status = 'pending';

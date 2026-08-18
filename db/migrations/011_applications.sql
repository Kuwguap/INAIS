-- Job/scholarship application pipeline, driven by what lands in the inbox.

create table if not exists applications (
    id              bigserial primary key,
    org             text        not null,
    role            text,
    kind            text        not null default 'job'
        check (kind in ('job', 'internship', 'scholarship', 'grant', 'program', 'other')),
    status          text        not null default 'applied'
        check (status in ('applied', 'assessment', 'interview', 'offer', 'rejected', 'withdrawn')),
    applied_at      timestamptz not null default now(),
    deadline        timestamptz,
    source_email_id bigint references email_events (id) on delete set null,
    task_id         bigint references tasks (id) on delete set null,
    notes           text,
    updated_at      timestamptz not null default now()
);

-- One row per org+role, so a later rejection or interview mail updates the existing
-- application instead of creating a duplicate.
create unique index if not exists applications_org_role_uidx
    on applications (lower(org), lower(coalesce(role, '')));
create index if not exists applications_status_idx on applications (status, updated_at desc);

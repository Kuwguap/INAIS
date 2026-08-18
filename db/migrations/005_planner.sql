-- Planner: tasks, reminders, pomodoro sessions (M8)

create table if not exists tasks (
    id           bigserial primary key,
    context      text        not null default 'personal'
        check (context in ('school', 'work', 'personal')),
    title        text        not null,
    notes        text,
    due          timestamptz,
    priority     int         not null default 3,   -- 1 = highest
    status       text        not null default 'open'
        check (status in ('open', 'done', 'dropped')),
    created_at   timestamptz not null default now(),
    completed_at timestamptz
);
create index if not exists tasks_open_due_idx on tasks (due nulls last) where status = 'open';

create table if not exists reminders (
    id             bigserial primary key,
    text           text        not null,
    fire_at        timestamptz not null,
    recurring_cron text,                            -- 5-field cron; null = one-shot
    fired          boolean     not null default false,
    created_at     timestamptz not null default now()
);
create index if not exists reminders_due_idx on reminders (fire_at) where fired = false;

create table if not exists pomodoro_sessions (
    id         bigserial primary key,
    started_at timestamptz not null default now(),
    minutes    int         not null default 25,
    completed  boolean     not null default false,
    label      text,
    ended_at   timestamptz
);
create index if not exists pomodoro_recent_idx on pomodoro_sessions (started_at desc);

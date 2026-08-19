-- Alarm-grade reminders: a reminder is not delivered until the user says so.
-- fired = the first send happened; acknowledged = the user pressed Stop (or typed it).
-- Existing rows default to acknowledged so history does not start nagging on deploy.

alter table reminders add column if not exists acknowledged boolean not null default true;
alter table reminders add column if not exists nag_count int not null default 0;
alter table reminders add column if not exists nag_at timestamptz;
alter table reminders add column if not exists message_id bigint;

create index if not exists reminders_nag_idx on reminders (nag_at)
    where not acknowledged;

-- last_fired_at: when the alarm actually rang. fire_at cannot serve that role because a
-- recurring reminder's fire_at is re-armed to the NEXT occurrence while it is still ringing,
-- which made typed-stop pick the wrong reminder to silence.

alter table reminders add column if not exists last_fired_at timestamptz;

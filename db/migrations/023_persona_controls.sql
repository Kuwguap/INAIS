-- Runtime persona controls: the owner tunes tone/brevity/humour live via /persona, and the
-- choice persists across restarts. Text-valued (unlike the boolean runtime_flags), so it gets
-- its own tiny table. The .env persona_* settings remain the defaults when a key is absent.

create table if not exists persona_controls (
    key        text primary key,       -- 'tone' | 'brevity' | 'humour'
    value      text not null,
    updated_at timestamptz not null default now()
);

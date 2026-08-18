-- Interview and viva practice: a question bank plus what the user actually answered.

create table if not exists drill_questions (
    id          bigserial primary key,
    category    text        not null default 'behavioral'
        check (category in ('behavioral', 'technical', 'viva')),
    exam_id     bigint references exams (id) on delete cascade,
    question    text        not null,
    guidance    text,                      -- what a strong answer covers; grading rubric
    times_asked int         not null default 0,
    last_asked  timestamptz,
    created_at  timestamptz not null default now()
);
create index if not exists drill_pick_idx on drill_questions (category, times_asked);
create unique index if not exists drill_question_uidx on drill_questions (lower(question));

create table if not exists drill_answers (
    id          bigserial primary key,
    question_id bigint      not null references drill_questions (id) on delete cascade,
    transcript  text        not null,
    feedback    text        not null,
    created_at  timestamptz not null default now()
);

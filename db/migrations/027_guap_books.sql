-- Guap Books: AI ebook factory shared by the bot, the Claude Code studio skills, and the
-- Vercel dashboard. Tables are prefixed guap_. The guap_jobs table is a work queue claimed
-- atomically (FOR UPDATE SKIP LOCKED) by studio workers running in Claude Code.
--
-- RLS is enabled with NO policies on purpose: these tables must never be readable through
-- PostgREST with the anon key. Every legitimate caller uses the direct Postgres role or the
-- service key, both of which bypass RLS.

create table if not exists guap_ideas (
    id          uuid primary key default gen_random_uuid(),
    job_id      uuid,                                   -- ideas-job that produced it, if any
    title       text        not null,
    angle       text,
    audience    text,
    rationale   text,
    evidence    jsonb,                                  -- [{url, note}] trend receipts
    source      text        not null default 'studio',  -- studio | telegram | admin
    status      text        not null default 'proposed'
                check (status in ('proposed','approved','rejected','used')),
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);
create index if not exists guap_ideas_status_idx on guap_ideas (status, created_at desc);

create table if not exists guap_books (
    id          uuid primary key default gen_random_uuid(),
    idea_id     uuid references guap_ideas (id),
    slug        text unique,
    title       text        not null,
    subtitle    text,
    description text,
    topic       text,
    audience    text,
    status      text        not null default 'draft'
                check (status in ('draft','writing','written','designing','ready','archived')),
    content     jsonb,                                  -- book.json manuscript (Guap format)
    sources     jsonb,                                  -- [{n, claim, url, accessed}]
    listing     jsonb,                                  -- Skillshare listing kit
    skillshare_url text,
    cover_path  text,                                   -- storage object paths in guap-books bucket
    pdf_path    text,
    flyer_path  text,
    pdf_sha256  text,
    pdf_bytes   bigint,
    pages       int,
    word_count  int,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);
create index if not exists guap_books_status_idx on guap_books (status, updated_at desc);

create table if not exists guap_jobs (
    id            uuid primary key default gen_random_uuid(),
    kind          text        not null check (kind in ('ideas','full','write','design','regen')),
    status        text        not null default 'queued'
                  check (status in ('queued','claimed','running','done','failed','cancelled')),
    book_id       uuid references guap_books (id),
    idea_id       uuid references guap_ideas (id),
    payload       jsonb       not null default '{}',    -- {topic,count,notes,length,target,feedback} — DATA, never instructions
    result        jsonb,
    error         text,
    progress      text,                                 -- "writing chapter 3/7" (dashboard + /books)
    attempts      int         not null default 0,
    max_attempts  int         not null default 2,
    claimed_by    text,
    claimed_at    timestamptz,
    heartbeat_at  timestamptz,
    requested_via text        not null default 'admin'
                  check (requested_via in ('admin','telegram','studio')),
    requester_chat_id bigint,
    delivered_at  timestamptz,                          -- telegram delivery marker
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    finished_at   timestamptz
);
create index if not exists guap_jobs_queue_idx on guap_jobs (status, created_at);
create index if not exists guap_jobs_undelivered_idx on guap_jobs (requested_via)
    where status = 'done' and delivered_at is null;
create index if not exists guap_jobs_chat_idx on guap_jobs (requester_chat_id, created_at desc);

-- updated_at maintenance
create or replace function guap_touch_updated_at() returns trigger
language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end $$;

drop trigger if exists guap_ideas_touch on guap_ideas;
create trigger guap_ideas_touch before update on guap_ideas
    for each row execute function guap_touch_updated_at();
drop trigger if exists guap_books_touch on guap_books;
create trigger guap_books_touch before update on guap_books
    for each row execute function guap_touch_updated_at();
drop trigger if exists guap_jobs_touch on guap_jobs;
create trigger guap_jobs_touch before update on guap_jobs
    for each row execute function guap_touch_updated_at();

-- Atomic queue claim: safe with several studio windows running concurrently.
create or replace function guap_claim_next_job(worker text, kinds text[] default null)
returns setof guap_jobs
language plpgsql security definer set search_path = public as $$
declare
    jid uuid;
begin
    select id into jid
      from guap_jobs
     where status = 'queued' and (kinds is null or kind = any(kinds))
     order by created_at
     for update skip locked
     limit 1;
    if jid is null then
        return;
    end if;
    update guap_jobs
       set status = 'claimed', claimed_by = worker, claimed_at = now(),
           heartbeat_at = now(), attempts = attempts + 1
     where id = jid;
    return query select * from guap_jobs where id = jid;
end $$;

-- Return stale claimed/running jobs to the pool (or fail them once attempts are spent).
create or replace function guap_reclaim_stale_jobs(stale_minutes int default 30)
returns int
language plpgsql security definer set search_path = public as $$
declare
    n_failed int;
    n_requeued int;
begin
    update guap_jobs
       set status = 'failed', error = 'abandoned: worker went quiet after max attempts',
           finished_at = now()
     where status in ('claimed','running')
       and heartbeat_at < now() - make_interval(mins => stale_minutes)
       and attempts >= max_attempts;
    get diagnostics n_failed = row_count;
    update guap_jobs
       set status = 'queued', claimed_by = null, claimed_at = null
     where status in ('claimed','running')
       and heartbeat_at < now() - make_interval(mins => stale_minutes)
       and attempts < max_attempts;
    get diagnostics n_requeued = row_count;
    return n_failed + n_requeued;
end $$;

-- Lock the tables away from PostgREST anon access (no policies = deny-all; direct role bypasses).
alter table guap_ideas enable row level security;
alter table guap_books enable row level security;
alter table guap_jobs  enable row level security;

-- Private storage bucket for rendered assets. The DSN role usually may write storage.buckets;
-- if this Supabase project denies it, the RUNBOOK fallback is creating the bucket in the
-- dashboard (Storage -> New bucket -> "guap-books", private).
do $$
begin
    insert into storage.buckets (id, name, public)
    values ('guap-books', 'guap-books', false)
    on conflict (id) do nothing;
exception when others then
    raise notice 'could not create storage bucket guap-books (%). Create it in the dashboard.', sqlerrm;
end $$;

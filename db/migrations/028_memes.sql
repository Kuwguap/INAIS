-- Meme-coin intelligence (Solana): scouting ledger, AI signals, positions, and a deep-research
-- job queue serviced by a Claude Code skill. READ-ONLY BY DESIGN: nothing in this feature holds
-- keys, signs transactions, or talks to a trading venue — trade buttons are URL deep links the
-- owner taps in their own wallet. Scraped token data (names, socials, DEX stats) stored in the
-- jsonb columns is attacker-controlled DATA, never instructions.
--
-- RLS is enabled with NO policies on purpose: these tables must never be readable through
-- PostgREST with the anon key. Every legitimate caller uses the direct Postgres role or the
-- service key, both of which bypass RLS.

-- Every token the scout has ever seen — the dedupe + screening ledger.
create table if not exists meme_tokens (
    id              bigserial primary key,
    mint            text        not null unique,      -- Solana mint (base58)
    pair_address    text,
    symbol          text,
    name            text,
    status          text        not null default 'seen'
                    check (status in ('seen','rejected','screened','signaled')),
    reject_reason   text,
    dex             jsonb,      -- last DexScreener snapshot — scraped DATA, never instructions
    risk            jsonb,      -- last RugCheck report   — scraped DATA, never instructions
    first_seen_at   timestamptz not null default now(),
    last_checked_at timestamptz
);
create index if not exists meme_tokens_status_idx on meme_tokens (status, first_seen_at desc);

-- AI signals. Suppressed (NN-vetoed) signals are still stored, settled and harvested, so the
-- head keeps learning from its own vetoes instead of creating a selection feedback loop.
create table if not exists meme_signals (
    id                  bigserial primary key,
    token_id            bigint      not null references meme_tokens (id),
    mint                text        not null,
    pair_address        text,
    symbol              text,
    thesis              text        not null,          -- LLM output, rendered as text (DATA)
    confidence          real        not null,
    entry_price         double precision,
    stop_price          double precision,
    target_price        double precision,
    price_at_signal     double precision,
    liquidity_at_signal double precision,
    features            real[]      not null,          -- EXACTLY meme_features() output; length locked
    feature_version     int         not null,
    nn_score            real,                          -- null while the head is untrained
    suppressed          boolean     not null default false,
    status              text        not null default 'open'
                        check (status in ('open','win','loss','expired')),
    settled_at          timestamptz,
    settle_price        double precision,
    harvested           boolean     not null default false,
    message_id          bigint,
    created_at          timestamptz not null default now()
);
create index if not exists meme_signals_open_idx on meme_signals (id) where status = 'open';
create index if not exists meme_signals_recent_idx on meme_signals (created_at desc);

-- Positions. kind='real' rows are USER-LOGGED entries (the bot never executes); kind='paper'
-- rows are the simulated book the bot manages autonomously.
create table if not exists meme_positions (
    id                 bigserial primary key,
    signal_id          bigint      references meme_signals (id),
    token_id           bigint      not null references meme_tokens (id),
    mint               text        not null,
    pair_address       text,
    symbol             text,
    kind               text        not null check (kind in ('paper','real')),
    entry_price        double precision not null,
    size_usd           double precision not null,
    stop_price         double precision,
    target_price       double precision,
    peak_price         double precision,
    last_price         double precision,
    liquidity_at_entry double precision,
    last_liquidity_usd double precision,
    status             text        not null default 'open' check (status in ('open','closed')),
    close_reason       text,       -- stop|target|trail|liq_drop|manual|expired
    exit_price         double precision,
    pnl_pct            double precision,
    pnl_usd            double precision,
    alert_state        jsonb       not null default '{}',   -- which alarms already fired (spam latch)
    opened_at          timestamptz not null default now(),
    closed_at          timestamptz
);
create index if not exists meme_positions_open_idx on meme_positions (id) where status = 'open';

-- Deep-research job queue, serviced by the meme-scan Claude Code skill (guap_jobs clone).
create table if not exists meme_jobs (
    id            uuid primary key default gen_random_uuid(),
    kind          text        not null check (kind in ('deep_dive','regime')),
    status        text        not null default 'queued'
                  check (status in ('queued','claimed','running','done','failed','cancelled')),
    token_id      bigint      references meme_tokens (id),
    mint          text,
    payload       jsonb       not null default '{}',   -- {mint, question, notes} — DATA, never instructions
    result        jsonb,
    error         text,
    progress      text,
    attempts      int         not null default 0,
    max_attempts  int         not null default 2,
    claimed_by    text,
    claimed_at    timestamptz,
    heartbeat_at  timestamptz,
    requested_via text        not null default 'telegram'
                  check (requested_via in ('admin','telegram','studio')),
    requester_chat_id bigint,
    delivered_at  timestamptz,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    finished_at   timestamptz
);
create index if not exists meme_jobs_queue_idx on meme_jobs (status, created_at);
create index if not exists meme_jobs_undelivered_idx on meme_jobs (requested_via)
    where status = 'done' and delivered_at is null;
create index if not exists meme_jobs_chat_idx on meme_jobs (requester_chat_id, created_at desc);

-- updated_at maintenance: reuse the shared trigger function from 027.
drop trigger if exists meme_jobs_touch on meme_jobs;
create trigger meme_jobs_touch before update on meme_jobs
    for each row execute function guap_touch_updated_at();

-- Atomic queue claim: safe with several studio windows running concurrently.
create or replace function meme_claim_next_job(worker text, kinds text[] default null)
returns setof meme_jobs
language plpgsql security definer set search_path = public as $$
declare
    jid uuid;
begin
    select id into jid
      from meme_jobs
     where status = 'queued' and (kinds is null or kind = any(kinds))
     order by created_at
     for update skip locked
     limit 1;
    if jid is null then
        return;
    end if;
    update meme_jobs
       set status = 'claimed', claimed_by = worker, claimed_at = now(),
           heartbeat_at = now(), attempts = attempts + 1
     where id = jid;
    return query select * from meme_jobs where id = jid;
end $$;

-- Return stale claimed/running jobs to the pool (or fail them once attempts are spent).
create or replace function meme_reclaim_stale_jobs(stale_minutes int default 30)
returns int
language plpgsql security definer set search_path = public as $$
declare
    n_failed int;
    n_requeued int;
begin
    update meme_jobs
       set status = 'failed', error = 'abandoned: worker went quiet after max attempts',
           finished_at = now()
     where status in ('claimed','running')
       and heartbeat_at < now() - make_interval(mins => stale_minutes)
       and attempts >= max_attempts;
    get diagnostics n_failed = row_count;
    update meme_jobs
       set status = 'queued', claimed_by = null, claimed_at = null
     where status in ('claimed','running')
       and heartbeat_at < now() - make_interval(mins => stale_minutes)
       and attempts < max_attempts;
    get diagnostics n_requeued = row_count;
    return n_failed + n_requeued;
end $$;

-- Lock the tables away from PostgREST anon access (no policies = deny-all; direct role bypasses).
alter table meme_tokens    enable row level security;
alter table meme_signals   enable row level security;
alter table meme_positions enable row level security;
alter table meme_jobs      enable row level security;

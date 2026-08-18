-- Finance agent: hourly portfolio snapshots (trades table reserved for future syncing).

create table if not exists finance_snapshots (
    id         bigserial primary key,
    ts         timestamptz not null default now(),
    balances   jsonb       not null,
    total_usdt numeric     not null
);
create index if not exists finance_snapshots_ts_idx on finance_snapshots (ts desc);

create table if not exists trades (
    id       bigserial primary key,
    symbol   text    not null,
    trade_id bigint  not null,
    ts       timestamptz,
    side     text,
    qty      numeric,
    price    numeric,
    raw      jsonb,
    unique (symbol, trade_id)
);

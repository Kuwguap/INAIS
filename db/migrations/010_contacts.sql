-- People the user knows. Linked to a fact so contacts also surface through semantic search
-- ("who was the guy from the robotics lab?") rather than only exact-name lookup.

create table if not exists contacts (
    id           bigserial primary key,
    name         text        not null,
    org          text,
    how_met      text,
    last_contact timestamptz,
    follow_up_at date,
    notes        text,
    fact_id      bigint references facts (id) on delete set null,
    created_at   timestamptz not null default now()
);
create unique index if not exists contacts_name_uidx on contacts (lower(name));
create index if not exists contacts_followup_idx on contacts (follow_up_at)
    where follow_up_at is not null;

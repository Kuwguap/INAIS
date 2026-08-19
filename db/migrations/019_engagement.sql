-- The engagement head: learning WHEN speaking first actually lands.
--
-- proactive_log grows outcome columns so every unprompted message becomes a training
-- example with a genuine behavioural label: did the user reply within the window, or not?
-- nn_examples grows optional context features (time-of-day, weekday) because "will they
-- engage" depends on the clock as much as the content.

alter table proactive_log add column if not exists medium text not null default 'text'
    check (medium in ('text', 'voice'));
alter table proactive_log add column if not exists replied boolean;
alter table proactive_log add column if not exists reply_latency_s int;
alter table proactive_log add column if not exists harvested boolean not null default false;
create index if not exists proactive_unharvested_idx on proactive_log (sent_at)
    where not harvested;

alter table nn_examples add column if not exists context_features real[] not null default '{}';

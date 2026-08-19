-- Read-it-later: saved web pages live alongside PDFs in documents/doc_chunks, so everything
-- the user has given the assistant is searchable through one path.

alter table documents add column if not exists source_url text;
alter table documents add column if not exists kind text not null default 'pdf';
alter table documents add column if not exists summary text;

do $$
begin
    if not exists (select 1 from pg_constraint where conname = 'documents_kind_check') then
        alter table documents add constraint documents_kind_check
            check (kind in ('pdf', 'link', 'note'));
    end if;
end $$;

-- Saving the same URL twice should update, not duplicate.
create unique index if not exists documents_url_uidx on documents (source_url)
    where source_url is not null;
create index if not exists documents_kind_idx on documents (kind, uploaded_at desc);

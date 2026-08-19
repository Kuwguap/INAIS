-- Read-state for saved links, so the weekly reading digest only surfaces what's unread.
-- Links live as documents rows with kind='link'; read_at null = still in the queue.

alter table documents add column if not exists read_at timestamptz;

create index if not exists documents_unread_links_idx
    on documents (uploaded_at desc) where kind = 'link' and read_at is null;

-- AgriBridge migration 0012 — listing media storage
--
-- Browser clients do NOT upload directly.  The Flask API validates media and
-- writes with the service-role key, so authenticated users never receive a
-- storage.objects INSERT policy they could abuse to upload arbitrary files.

begin;

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'uploads',
  'uploads',
  true,
  27262976, -- 26 MiB: permits a 25 MiB video plus normal encoding overhead
  array['image/jpeg', 'image/png', 'image/webp', 'video/mp4', 'video/webm', 'video/quicktime']::text[]
)
on conflict (id) do update
set public = excluded.public,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

-- Remove historical browser-write policies. The backend service role bypasses
-- RLS; public visitors can retrieve only the public listing media.
drop policy if exists uploads_select on storage.objects;
drop policy if exists uploads_insert on storage.objects;
drop policy if exists uploads_update on storage.objects;
drop policy if exists uploads_public_read on storage.objects;

create policy uploads_public_read
on storage.objects
for select
to public
using (bucket_id = 'uploads');

commit;

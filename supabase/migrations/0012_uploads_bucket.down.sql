-- Migration 0012 rollback intentionally preserves media files and the bucket.
-- Removing a bucket would delete farmer listing photos and is not a safe rollback.
begin;
drop policy if exists uploads_public_read on storage.objects;
commit;

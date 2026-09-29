-- 011_r2_storage.sql
-- Medya Cloudflare R2'de: istek kaydı işlenmeyi bekleyen nesnenin anahtarını tutar (işlenince silinir).
-- media_evidence.path ve hash_blocklist.preview_path artık R2 nesne anahtarıdır.
ALTER TABLE moderation_requests ADD COLUMN media_key text;
CREATE INDEX idx_requests_media_key ON moderation_requests (media_key) WHERE media_key IS NOT NULL;
ALTER TABLE platform_settings DROP COLUMN IF EXISTS media_min_free_gb;

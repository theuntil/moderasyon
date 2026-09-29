-- 010_media_storage.sql
-- Kanıt görsellerinin ayrı ve kısa saklama süresi. Orijinal medya zaten hiç saklanmaz.
--   0: otomatik engellenen içerik için kanıt hiç saklanmaz (sadece inceleme bekleyenler için tutulur)
ALTER TABLE platform_settings
    ADD COLUMN evidence_retention_hours int NOT NULL DEFAULT 72 CHECK (evidence_retention_hours BETWEEN 0 AND 8760);

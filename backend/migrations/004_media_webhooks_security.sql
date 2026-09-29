-- 004_media_webhooks_security.sql
-- Görsel/video moderasyonu, kanıt kareleri, görsel engel listesi (perceptual hash), webhook'lar,
-- veri saklama süresi, yönetici 2FA ve kuyruk kurtarma iyileştirmeleri.

-- ---------------------------------------------------------------- istekler
ALTER TABLE moderation_requests
    ADD COLUMN source           text CHECK (source IN ('json', 'upload', 'url')),
    ADD COLUMN media_mime       text,
    ADD COLUMN media_bytes      bigint,
    ADD COLUMN media_width      int,
    ADD COLUMN media_height     int,
    ADD COLUMN media_duration_ms int,
    ADD COLUMN media_frames     int,
    ADD COLUMN media_sha256     text,
    ADD COLUMN content_purged_at timestamptz,      -- saklama süresi dolup içerik silindiğinde
    ADD COLUMN last_enqueued_at timestamptz;       -- takılan iş kurtarma: aynı işi her dakika tekrar kuyruğa atmamak için

CREATE INDEX idx_requests_media_sha ON moderation_requests (media_sha256) WHERE media_sha256 IS NOT NULL;
CREATE INDEX idx_requests_purge ON moderation_requests (created_at) WHERE content_purged_at IS NULL;

-- ---------------------------------------------------------------- kanıt (review için saklanan küçük kopyalar)
-- Orijinal dosya işlendikten sonra silinir. Sadece izin verilmeyen içerikler için küçültülmüş,
-- EXIF'i temizlenmiş kopyalar saklanır ve saklama süresi dolunca silinir.
CREATE TABLE media_evidence (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id   uuid NOT NULL REFERENCES moderation_requests(id) ON DELETE CASCADE,
    kind         text NOT NULL CHECK (kind IN ('image', 'frame')),
    path         text NOT NULL,                 -- media_dir'e göre göreli yol
    timestamp_ms int,                           -- videoda karenin zamanı
    width        int,
    height       int,
    phash        bigint,                        -- NULL: bilgi taşımayan (düz renk vb.) kare
    categories   jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_evidence_request ON media_evidence (request_id);

-- ---------------------------------------------------------------- görsel engel listesi
-- Moderatörün engellediği bir görsel/video karesi tekrar yüklenirse (yeniden boyutlandırılmış,
-- sıkıştırılmış olsa bile) perceptual hash benzerliğiyle otomatik engellenir.
CREATE TABLE hash_blocklist (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id        uuid REFERENCES projects(id) ON DELETE CASCADE,   -- NULL: tüm platform
    sha256            text,
    phashes           bigint[] NOT NULL DEFAULT '{}',
    reason            text,
    source_public_id  text,
    preview_path      text,
    created_by        uuid REFERENCES admin_users(id) ON DELETE SET NULL,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_blocklist_sha ON hash_blocklist (sha256) WHERE sha256 IS NOT NULL;

-- ---------------------------------------------------------------- webhook'lar
ALTER TABLE projects
    ADD COLUMN webhook_url     text,
    ADD COLUMN webhook_secret  text,       -- HMAC-SHA256 imza anahtarı
    ADD COLUMN webhook_enabled boolean NOT NULL DEFAULT false;

CREATE TABLE webhook_deliveries (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    event         text NOT NULL,
    request_public_id text,
    payload       jsonb NOT NULL,
    status        text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'delivered', 'failed')),
    attempts      int NOT NULL DEFAULT 0,
    last_status_code int,
    last_error    text,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    delivered_at  timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_webhook_pending ON webhook_deliveries (next_attempt_at) WHERE status = 'pending';
CREATE INDEX idx_webhook_project ON webhook_deliveries (project_id, created_at DESC);

-- ---------------------------------------------------------------- saklama süresi (KVKK)
ALTER TABLE platform_settings
    ADD COLUMN retention_days int NOT NULL DEFAULT 30 CHECK (retention_days BETWEEN 1 AND 3650);

-- ---------------------------------------------------------------- yönetici 2FA (TOTP)
ALTER TABLE admin_users
    ADD COLUMN totp_secret        text,          -- etkinleşene kadar pending olarak tutulur
    ADD COLUMN totp_enabled       boolean NOT NULL DEFAULT false,
    ADD COLUMN totp_last_step     bigint,        -- aynı kodun iki kez kullanılmasını engeller
    ADD COLUMN recovery_codes     text[] NOT NULL DEFAULT '{}';   -- SHA-256 hash'leri

-- İki adımlı girişin ara durumu (şifre doğru, kod bekleniyor)
CREATE TABLE admin_mfa_challenges (
    token_hash  text PRIMARY KEY,
    user_id     uuid NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
    attempts    int NOT NULL DEFAULT 0,
    expires_at  timestamptz NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

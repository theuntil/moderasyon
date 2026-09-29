-- 001_init.sql
-- Faz 1 çekirdek şema. Her tenant verisi project_id taşır.
-- Bu dosyalar API container'ı başlarken app/migrate.py tarafından sırayla uygulanır.

CREATE TABLE projects (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL,
    slug        text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
    description text,
    status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    -- NULL ise ortam değişkenindeki varsayılan limit kullanılır
    rate_limit_per_second int CHECK (rate_limit_per_second > 0),
    rate_limit_per_minute int CHECK (rate_limit_per_minute > 0),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE project_api_keys (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id   uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name         text NOT NULL,                       -- "KAYS Production"
    key_prefix   text NOT NULL,                       -- panelde gösterilecek kısım: "mk_live_Ab3d"
    key_hash     text NOT NULL UNIQUE,                -- HMAC-SHA256(pepper, key). Plaintext asla saklanmaz.
    environment  text NOT NULL CHECK (environment IN ('live', 'test')),
    status       text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled', 'revoked')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    expires_at   timestamptz,
    revoked_at   timestamptz,
    created_by   uuid                                 -- admin users tablosu gelince FK olacak
);
CREATE INDEX idx_api_keys_project ON project_api_keys (project_id);

CREATE TABLE moderation_requests (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    public_id           text NOT NULL UNIQUE,         -- dışarıya gösterilen id: "mod_..."
    project_id          uuid NOT NULL REFERENCES projects(id),
    api_key_id          uuid NOT NULL REFERENCES project_api_keys(id),
    content_type        text NOT NULL CHECK (content_type IN ('text', 'image', 'video')),
    content_text        text,                         -- TODO: saklama süresi dolunca temizlenecek (KVKK)
    content_url         text,
    external_user_id    text,
    external_content_id text,
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key     text,
    request_hash        text,                         -- aynı Idempotency-Key farklı body ile gelirse 409 için
    status              text NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'processing', 'completed', 'failed')),
    attempts            int NOT NULL DEFAULT 0,
    error               text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    started_at          timestamptz,
    completed_at        timestamptz,
    -- Aynı project içinde aynı Idempotency-Key ikinci kez işlenmez.
    -- NULL değerler çakışmaz, yani key göndermeyen istekler etkilenmez.
    CONSTRAINT uq_request_idempotency UNIQUE (project_id, idempotency_key)
);
CREATE INDEX idx_requests_project_created ON moderation_requests (project_id, created_at DESC);
CREATE INDEX idx_requests_stuck ON moderation_requests (status, created_at)
    WHERE status IN ('queued', 'processing');

CREATE TABLE moderation_results (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id         uuid NOT NULL UNIQUE REFERENCES moderation_requests(id) ON DELETE CASCADE,
    project_id         uuid NOT NULL REFERENCES projects(id),
    decision           text NOT NULL CHECK (decision IN ('allow', 'review', 'block')),
    categories         jsonb NOT NULL DEFAULT '[]'::jsonb,   -- [{"name": "harassment", "score": 0.9}]
    max_score          real NOT NULL DEFAULT 0,
    reason             text NOT NULL,
    providers          jsonb NOT NULL DEFAULT '[]'::jsonb,   -- [{"provider": "...", "model": "...", "version": "..."}]
    policy_version     text NOT NULL,
    processing_time_ms int NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_results_project_created ON moderation_results (project_id, created_at DESC);

CREATE TABLE review_queue (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id     uuid NOT NULL REFERENCES projects(id),
    request_id     uuid NOT NULL UNIQUE REFERENCES moderation_requests(id) ON DELETE CASCADE,
    status         text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'resolved')),
    ai_decision    text NOT NULL CHECK (ai_decision IN ('allow', 'review', 'block')),
    human_decision text CHECK (human_decision IN ('allow', 'block')),
    reviewed_by    uuid,
    reviewed_at    timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_review_pending ON review_queue (project_id, status, created_at);

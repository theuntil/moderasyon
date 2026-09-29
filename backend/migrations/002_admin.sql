-- 002_admin.sql
-- Admin paneli: kullanıcılar, oturumlar, audit log, IP kuralları, platform ayarları, policy versiyonları.

CREATE TABLE admin_users (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email                text NOT NULL,
    name                 text,
    password_hash        text NOT NULL,
    role                 text NOT NULL CHECK (role IN ('owner', 'admin', 'moderator', 'viewer')),
    status               text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    must_change_password boolean NOT NULL DEFAULT false,
    failed_logins        int NOT NULL DEFAULT 0,
    locked_until         timestamptz,
    last_login_at        timestamptz,
    last_login_ip        inet,
    password_changed_at  timestamptz NOT NULL DEFAULT now(),
    created_at           timestamptz NOT NULL DEFAULT now(),
    created_by           uuid
);
CREATE UNIQUE INDEX uq_admin_users_email ON admin_users (lower(email));

CREATE TABLE admin_sessions (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
    token_hash   text NOT NULL UNIQUE,              -- cookie'deki token'ın SHA-256'sı; token'ın kendisi saklanmaz
    ip           inet,
    user_agent   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    revoked_at   timestamptz
);
CREATE INDEX idx_admin_sessions_user ON admin_sessions (user_id);

-- Project silinse bile audit kaydı kalsın diye project_id'de FK yok.
CREATE TABLE audit_logs (
    id          bigserial PRIMARY KEY,
    actor_id    uuid REFERENCES admin_users(id) ON DELETE SET NULL,
    actor_email text,
    action      text NOT NULL,
    project_id  uuid,
    target_type text,
    target_id   text,
    details     jsonb NOT NULL DEFAULT '{}'::jsonb,
    ip          inet,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_created ON audit_logs (created_at DESC);
CREATE INDEX idx_audit_project ON audit_logs (project_id, created_at DESC);

-- project_id NULL ise kural tüm platform için geçerlidir.
CREATE TABLE ip_rules (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid REFERENCES projects(id) ON DELETE CASCADE,
    cidr       cidr NOT NULL,
    reason     text,
    created_by uuid REFERENCES admin_users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz
);
CREATE INDEX idx_ip_rules_cidr ON ip_rules USING gist (cidr inet_ops);
CREATE INDEX idx_ip_rules_project ON ip_rules (project_id);

-- Tek satırlık platform ayarları tablosu
CREATE TABLE platform_settings (
    id                           int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    service_enabled              boolean NOT NULL DEFAULT true,
    maintenance_message          text,
    threshold_review             double precision NOT NULL DEFAULT 0.50 CHECK (threshold_review > 0 AND threshold_review < 1),
    threshold_block              double precision NOT NULL DEFAULT 0.85 CHECK (threshold_block > 0 AND threshold_block <= 1),
    policy_version               int NOT NULL DEFAULT 1,
    default_rate_limit_per_second int NOT NULL DEFAULT 20 CHECK (default_rate_limit_per_second > 0),
    default_rate_limit_per_minute int NOT NULL DEFAULT 600 CHECK (default_rate_limit_per_minute > 0),
    updated_at                   timestamptz NOT NULL DEFAULT now(),
    updated_by                   uuid REFERENCES admin_users(id) ON DELETE SET NULL,
    CHECK (threshold_review < threshold_block)
);
INSERT INTO platform_settings (id) VALUES (1);

CREATE TABLE policy_versions (
    version          int PRIMARY KEY,
    threshold_review double precision NOT NULL,
    threshold_block  double precision NOT NULL,
    created_by       uuid REFERENCES admin_users(id) ON DELETE SET NULL,
    created_at       timestamptz NOT NULL DEFAULT now()
);
INSERT INTO policy_versions (version, threshold_review, threshold_block) VALUES (1, 0.50, 0.85);

-- İsteğin geldiği IP (key'in hangi IP'lerden kullanıldığını görmek ve IP ban için)
ALTER TABLE moderation_requests ADD COLUMN client_ip inet;
CREATE INDEX idx_requests_key_ip ON moderation_requests (api_key_id, created_at DESC) WHERE client_ip IS NOT NULL;

-- Panel filtreleri için
CREATE INDEX idx_results_project_decision ON moderation_results (project_id, decision, created_at DESC);
CREATE INDEX idx_requests_created ON moderation_requests (created_at DESC);

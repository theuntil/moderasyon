-- 008_autoban.sql
-- Akıllı IP koruması: olağandışı trafik gönderen IP'ler otomatik ve kademeli olarak banlanır.

ALTER TABLE platform_settings
    ADD COLUMN autoban_mode              text NOT NULL DEFAULT 'enforce' CHECK (autoban_mode IN ('off', 'monitor', 'enforce')),
    ADD COLUMN autoban_auth_fail_limit   int  NOT NULL DEFAULT 50   CHECK (autoban_auth_fail_limit > 0),    -- 5 dk
    ADD COLUMN autoban_scan_limit        int  NOT NULL DEFAULT 30   CHECK (autoban_scan_limit > 0),         -- 5 dk
    ADD COLUMN autoban_flood_limit       int  NOT NULL DEFAULT 6000 CHECK (autoban_flood_limit > 0),        -- 1 dk
    ADD COLUMN autoban_panel_login_limit int  NOT NULL DEFAULT 20   CHECK (autoban_panel_login_limit > 0),  -- 15 dk
    ADD COLUMN autoban_allowlist         text[] NOT NULL DEFAULT '{}';                                      -- asla banlanmaz

-- Kural kaynağı: panelden elle mi, otomatik mi
ALTER TABLE ip_rules ADD COLUMN source text NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'auto'));
CREATE INDEX idx_ip_rules_auto ON ip_rules (cidr) WHERE source = 'auto';

-- Otomatik koruma olayları (izleme modunda da kaydedilir)
CREATE TABLE autoban_events (
    id         bigserial PRIMARY KEY,
    ip         inet NOT NULL,
    rule       text NOT NULL,          -- auth_fail | scan | flood | panel_login
    count      int  NOT NULL,
    action     text NOT NULL,          -- banned | monitored | skipped_allowlist
    minutes    int,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_autoban_events_ip ON autoban_events (ip, created_at DESC);
CREATE INDEX idx_autoban_events_created ON autoban_events (created_at DESC);

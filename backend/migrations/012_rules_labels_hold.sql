-- 012_rules_labels_hold.sql
-- Özel yasaklı kelimeler, yasaklı görsel/semboller, etiketli kararlar, yasal saklama ve kullanıcı bilgileri.

-- Yasaklı kelimeler / ifadeler (project_id NULL: tüm platform)
CREATE TABLE custom_terms (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  uuid REFERENCES projects(id) ON DELETE CASCADE,
    term        text NOT NULL,
    normalized  text NOT NULL,
    match_mode  text NOT NULL DEFAULT 'word' CHECK (match_mode IN ('word', 'contains')),
    label       text NOT NULL,                          -- uygulamaya ve panele dönen etiket, ör. "terör"
    severity    text NOT NULL DEFAULT 'block' CHECK (severity IN ('block', 'critical')),
    created_by  uuid REFERENCES admin_users(id) ON DELETE SET NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_custom_terms ON custom_terms (coalesce(project_id, '00000000-0000-0000-0000-000000000000'::uuid), normalized);

-- Yasaklı görsel içerik / semboller (AI görsel kontrolü ile; tüm platform)
CREATE TABLE visual_rules (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    label       text NOT NULL,                          -- ör. "terör:pkk_sembolü"
    description text NOT NULL,                          -- modele ne arayacağını anlatan tarif
    severity    text NOT NULL DEFAULT 'critical' CHECK (severity IN ('block', 'critical')),
    enabled     boolean NOT NULL DEFAULT true,
    created_by  uuid REFERENCES admin_users(id) ON DELETE SET NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
-- Varsayılan: sadece terör örgütü sembolleri (diğer kurallar panelden eklenir)
INSERT INTO visual_rules (label, description, severity) VALUES
 ('terör:pkk_kck', 'Symbols, flags, emblems or propaganda of PKK, KCK, PYD or YPG (e.g. the PKK red star on a yellow disc, KCK/YPG flags and banners), or posters/portraits presented as their leader imagery with such symbols', 'critical'),
 ('terör:işid', 'ISIS / DAESH black flag, emblem or propaganda imagery', 'critical');

-- Kararın etiketleri ve önemi
ALTER TABLE moderation_results
    ADD COLUMN labels   text[] NOT NULL DEFAULT '{}',
    ADD COLUMN severity text NOT NULL DEFAULT 'normal' CHECK (severity IN ('normal', 'critical'));
CREATE INDEX idx_results_labels ON moderation_results USING gin (labels);
CREATE INDEX idx_results_critical ON moderation_results (created_at DESC) WHERE severity = 'critical';

-- Yasal saklama: kritik içerik (ve orijinal medyası) otomatik silinmez; sadece yönetici siler
ALTER TABLE moderation_requests
    ADD COLUMN legal_hold    boolean NOT NULL DEFAULT false,
    ADD COLUMN held_at       timestamptz,
    ADD COLUMN hold_media_key text,                     -- R2 hold/ altında orijinal dosya
    ADD COLUMN user_info_enc text;                      -- ad, soyad, e-posta, telefon... (AES-GCM şifreli)
CREATE INDEX idx_requests_hold ON moderation_requests (held_at DESC) WHERE legal_hold;

ALTER TABLE platform_settings
    ADD COLUMN legal_hold_enabled   boolean NOT NULL DEFAULT true,
    ADD COLUMN ai_sensitive_media   boolean NOT NULL DEFAULT true,   -- belirsiz çıplaklıkta AI'a danış
    ADD COLUMN visual_rules_enabled boolean NOT NULL DEFAULT true;

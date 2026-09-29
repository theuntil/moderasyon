-- 006_ai_budget.sql
-- AI (Layer 2) yönetimi panele taşınır: sağlayıcı, model, limitler, bütçeler, fiyat tablosu,
-- kullanım/maliyet kaydı ve bütçe uyarıları. API anahtarı güvenlik için env'de kalır.

ALTER TABLE platform_settings
    ADD COLUMN ai_enabled              boolean NOT NULL DEFAULT true,     -- genel acil durdurma anahtarı
    ADD COLUMN ai_provider             text    NOT NULL DEFAULT 'openai_moderation'
                                        CHECK (ai_provider IN ('openai_moderation', 'chat')),
    ADD COLUMN ai_model                text    NOT NULL DEFAULT 'gpt-5-nano',
    ADD COLUMN ai_vision_model         text    NOT NULL DEFAULT 'gpt-5-nano',
    ADD COLUMN ai_max_calls_per_minute int     NOT NULL DEFAULT 120 CHECK (ai_max_calls_per_minute >= 0),
    ADD COLUMN ai_daily_budget_usd     numeric(12, 4) NOT NULL DEFAULT 5  CHECK (ai_daily_budget_usd >= 0),
    ADD COLUMN ai_monthly_budget_usd   numeric(12, 4) NOT NULL DEFAULT 50 CHECK (ai_monthly_budget_usd >= 0),
    ADD COLUMN ai_alert_percent        int     NOT NULL DEFAULT 80 CHECK (ai_alert_percent BETWEEN 1 AND 100);

-- Proje bazında aylık AI bütçesi (NULL: sınır yok, sadece platform bütçesi geçerli)
ALTER TABLE projects ADD COLUMN ai_monthly_budget_usd numeric(12, 4) CHECK (ai_monthly_budget_usd >= 0);

-- Model fiyatları (USD / 1M token). Fiyatı tanımlı olmayan ücretli modelle çağrı yapılmaz.
CREATE TABLE ai_model_prices (
    model         text PRIMARY KEY,
    input_per_1m  numeric(12, 6) NOT NULL CHECK (input_per_1m >= 0),
    output_per_1m numeric(12, 6) NOT NULL CHECK (output_per_1m >= 0),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    uuid REFERENCES admin_users(id) ON DELETE SET NULL
);
INSERT INTO ai_model_prices (model, input_per_1m, output_per_1m) VALUES
    ('omni-moderation-latest', 0, 0),        -- OpenAI Moderation API ücretsiz
    ('gpt-5-nano',   0.05, 0.40),
    ('gpt-5-mini',   0.25, 2.00),
    ('gpt-5.4-nano', 0.20, 1.25),
    ('gpt-5.6-luna', 0.20, 1.20);

-- Günlük toplamlar: bütçe kontrolü bu tablodan yapılır (hızlı ve kesin).
-- reserved_usd: yanıtı beklenen çağrılar için ayrılmış tahmini tutar (eşzamanlı çağrılar bütçeyi aşamaz).
CREATE TABLE ai_usage_daily (
    day              date   NOT NULL,
    project_id       uuid   NOT NULL,    -- 00000000-0000-0000-0000-000000000000: panel test çağrıları
    model            text   NOT NULL,
    calls            int    NOT NULL DEFAULT 0,
    errors           int    NOT NULL DEFAULT 0,
    skipped          int    NOT NULL DEFAULT 0,   -- bütçe/limit/devre kesici nedeniyle yapılmayan
    input_tokens     bigint NOT NULL DEFAULT 0,
    output_tokens    bigint NOT NULL DEFAULT 0,
    cost_usd         numeric(14, 6) NOT NULL DEFAULT 0,
    reserved_usd     numeric(14, 6) NOT NULL DEFAULT 0,
    latency_ms_total bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (day, project_id, model)
);

-- Tek tek çağrılar (son 30 gün tutulur): hata ayıklama ve denetim için
CREATE TABLE ai_calls (
    id            bigserial PRIMARY KEY,
    project_id    uuid NOT NULL,
    request_id    uuid,
    model         text NOT NULL,
    kind          text NOT NULL,              -- text | image
    status        text NOT NULL,              -- ok | error | timeout | rate_limited | auth_error
    input_tokens  int NOT NULL DEFAULT 0,
    output_tokens int NOT NULL DEFAULT 0,
    cost_usd      numeric(14, 6) NOT NULL DEFAULT 0,
    latency_ms    int,
    error         text,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_ai_calls_created ON ai_calls (created_at DESC);

-- Uyarılar: aynı dönem için aynı uyarı bir kez oluşur
CREATE TABLE ai_alerts (
    id         bigserial PRIMARY KEY,
    kind       text NOT NULL,     -- daily_threshold | daily_exhausted | monthly_threshold | monthly_exhausted | auth_error | no_price
    period     text NOT NULL,     -- 2026-09-28 | 2026-09 | tarih-saat
    message    text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, period)
);

-- 005_project_policy_ai.sql
-- Proje bazında policy (kategori aksiyonları ve eşikleri) ve Layer 2 AI modu.

-- policy örneği:
-- {"categories": {"suggestive": {"action": "block"}, "harassment": {"review": 0.4, "block": 0.8}},
--  "ai_mode": "smart"}
-- action: default (eşiklere göre) | allow (yok say) | review (en fazla incele) | block (inceleme eşiğini geçerse engelle)
ALTER TABLE projects
    ADD COLUMN policy          jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN policy_revision int   NOT NULL DEFAULT 0;

-- Layer 2 çağrılarının kaydı (maliyet/performans takibi ve kalite ölçümü için)
ALTER TABLE moderation_results
    ADD COLUMN layer1_decision text CHECK (layer1_decision IN ('allow', 'review', 'block')),
    ADD COLUMN ai_used         boolean NOT NULL DEFAULT false,
    ADD COLUMN ai_latency_ms   int;
CREATE INDEX idx_results_ai ON moderation_results (created_at DESC) WHERE ai_used;

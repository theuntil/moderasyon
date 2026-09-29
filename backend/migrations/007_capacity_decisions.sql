-- 007_capacity_decisions.sql
-- Varsayılan hız sınırları eşzamanlı yüksek trafik için yükseltilir (sadece hiç değiştirilmemişse).
-- Karar modu (iki/üç adım), belirsiz içerik davranışı, AI tetikleme eşiği ve yedek karar proje
-- policy'sinde (projects.policy jsonb) tutulur; varsayılanlar kodda: iki adım, belirsiz → engelle,
-- AI risk ≥ 0.50 olunca, yedek karar yok.
UPDATE platform_settings
SET default_rate_limit_per_second = 100, default_rate_limit_per_minute = 3000
WHERE id = 1 AND default_rate_limit_per_second = 20 AND default_rate_limit_per_minute = 600;

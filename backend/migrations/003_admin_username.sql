-- 003_admin_username.sql
-- Panel girişi kullanıcı adıyla yapılır; e-posta isteğe bağlı hale gelir.

ALTER TABLE admin_users ADD COLUMN username text;
UPDATE admin_users SET username = lower(email);
ALTER TABLE admin_users ALTER COLUMN username SET NOT NULL;
ALTER TABLE admin_users ALTER COLUMN email DROP NOT NULL;
CREATE UNIQUE INDEX uq_admin_users_username ON admin_users (lower(username));

-- audit_logs.actor_email artık kullanıcı adını tutar (kolon adı geriye dönük uyumluluk için aynı kaldı)
COMMENT ON COLUMN audit_logs.actor_email IS 'İşlemi yapan yöneticinin kullanıcı adı';

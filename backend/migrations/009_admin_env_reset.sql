-- 009_admin_env_reset.sql
-- ADMIN_RESET_PASSWORD=true ile env'den yapılan şifre sıfırlamanın parmak izi: aynı env şifresiyle
-- sıfırlama sadece bir kez uygulanır (sonradan panelde değiştirilen şifre yeniden başlatmada ezilmez).
ALTER TABLE admin_users ADD COLUMN env_reset_fp text;

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    redis_url: str = "redis://redis:6379/0"

    # API key hash'i için gizli değer. Değişirse mevcut tüm key'ler geçersiz olur.
    api_key_pepper: str = Field(min_length=32)

    # --- Public API sınırları
    max_body_bytes: int = 256 * 1024          # JSON istekleri
    max_text_length: int = 20_000
    max_metadata_bytes: int = 4_096
    sync_wait_timeout_s: float = 8.0           # ?wait=true metin (AI dahil); süre dolarsa API kendisi işler
    media_sync_wait_timeout_s: float = 12.0    # ?wait=true görsel/video
    api_docs_enabled: bool = False             # /docs ve /openapi.json (güvenlik: varsayılan kapalı, API_DOCS_ENABLED=true ile açılır)
    request_timeout_s: float = 25.0            # hiçbir istek bundan uzun sürmez; süre dolarsa JSON yanıt döner

    # --- Kapasite (8 vCPU sunucu için varsayılanlar; eşzamanlı 100+ istek)
    db_pool_size: int = 15                     # API işçisi başına veritabanı bağlantısı
    worker_max_jobs: int = 100                 # metin işçisinin aynı anda işlediği iş (AI beklerken bile)
    media_worker_max_jobs: int = 3

    # Geçersiz anahtarla gelen istekler: IP başına dakikada bu kadar hatadan sonra 429
    auth_fail_limit_per_minute: int = 30

    # Bu ağlardan gelen X-Forwarded-For girdileri proxy (Traefik/nginx) kabul edilir;
    # istemci IP'si, zincirde sağdan ilk güvenilmeyen adrestir. Sahte başlıkla IP ban atlatılamaz.
    # Domain Cloudflare üzerinden geçiyorsa gerçek ziyaretçi IP'si CF-Connecting-IP'den okunur
    # (sadece bağlantı Cloudflare'in resmi IP aralığından geliyorsa)
    trust_cloudflare: bool = True
    trusted_proxies: str = "127.0.0.1/32,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"

    # --- Cloudflare R2 (tüm medya burada; sunucu diskine medya yazılmaz)
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = ""
    r2_jurisdiction: str = ""                  # boş | eu (AB veri yerleşimi için "eu" bucket)
    r2_endpoint: str = ""                      # boşsa hesap kimliğinden üretilir; testte yerel S3 adresi
    r2_create_bucket: bool = False             # sadece test/geliştirme: bucket yoksa oluştur

    # --- Görsel / video
    max_image_bytes: int = 20 * 1024 * 1024
    max_video_bytes: int = 200 * 1024 * 1024
    max_video_seconds: int = 600
    max_image_pixels: int = 60_000_000         # decompression bomb koruması
    video_max_frames: int = 24                 # videodan analiz edilecek en fazla kare
    ocr_enabled: bool = True                   # görseldeki yazıyı okuyup metin filtresinden geçir
    fetch_timeout_s: float = 20.0              # URL'den medya indirme
    fetch_max_redirects: int = 3

    # SADECE lokal geliştirme/test: iç ağ adreslerine medya indirme ve webhook gönderimine izin verir.
    # Production'da asla true yapmayın (SSRF korumasını kapatır).
    allow_private_network: bool = False

    # --- Layer 2: AI. Sağlayıcı, model, limit ve bütçeler PANELDEN yönetilir (AI ve maliyet sayfası).
    # Burada sadece altyapı ve gizli bilgiler var. ai_api_key boşsa (ve ai_base_url da boşsa) AI kapalıdır.
    ai_base_url: str = ""                          # boş + api key varsa: https://api.openai.com/v1
    ai_api_key: str = ""
    ai_moderation_model: str = "omni-moderation-latest"
    ai_timeout_s: float = 20.0

    # --- Webhook
    webhook_timeout_s: float = 10.0

    # --- Admin paneli
    panel_cookie_secure: bool = True            # lokal HTTP geliştirmede false yapılır
    panel_timezone: str = "Europe/Istanbul"     # istatistiklerde gün/saat sınırları
    panel_session_hours: int = 12               # oturumun mutlak ömrü
    panel_idle_minutes: int = 120               # bu süre işlem yapılmazsa oturum düşer

    # İlk yönetici: hiç yönetici yokken admin-api açılışta bu bilgilerle bir owner oluşturur.
    # İlk yönetici (panel girişi e-postayla). ADMIN_USERNAME eski kurulumlar için: o hesabın e-postası
    # boşsa açılışta ADMIN_EMAIL'e bağlanır.
    admin_email: str | None = None
    admin_username: str | None = None
    admin_password: str | None = None
    # true: ADMIN_EMAIL hesabının şifresi ADMIN_PASSWORD'e sıfırlanır, kilidi açılır, hesap etkinleşir
    # (aynı şifreyle yalnızca bir kez). Şifreyi unutunca veya giriş yapılamayınca kullanın.
    admin_reset_password: bool = False


settings = Settings()

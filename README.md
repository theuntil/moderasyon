# Moderation Platform

Birden fazla uygulamanın (proje) ortak kullandığı, self-hosted içerik moderasyon platformu.
Uygulama API key ile içerik gönderir, karar (`allow` / `review` / `block`) geri gelir.
Her şey admin panelinden yönetilir.

| Servis | Ne yapar | Dışarıya açık mı |
|---|---|---|
| `api` | Public moderasyon API'si | Evet → `moderation.<domain>` port 8000 |
| `panel` | Admin paneli (React + nginx) | Evet → `panel.<domain>` port 80 |
| `admin-api` | Panelin arka ucu | **Hayır**, sadece panel üzerinden |
| `worker` | Metin analizi, webhook teslimatı, bakım işleri | Hayır |
| `media-worker` | Görsel/video analizi (ffmpeg, OCR, NudeNet) | Hayır |
| `postgres`, `redis` | Veri ve kuyruk | Hayır |

## Dokploy ile kurulum

1. Repoyu Dokploy'da **Compose** servisi olarak ekle. Compose yolu: `docker-compose.yml`.
2. **Environment** sekmesine `.env.example`'daki değişkenleri kendi değerlerinle yapıştır.
   Şifreler için: `openssl rand -hex 32`.
3. **Deploy**. Veritabanı tabloları ilk açılışta otomatik oluşur.
4. **Domains** sekmesi:
   - servis `api`, port `8000`, domain `moderation.<domain>`, HTTPS açık
   - servis `panel`, port `80`, domain `panel.<domain>`, HTTPS açık
   - `admin-api` servisine domain **bağlama**.
5. DNS'te `panel.<domain>` için sunucu IP'sine A kaydı ekle.
6. `https://panel.<domain>` adresinde env'deki `ADMIN_EMAIL` / `ADMIN_PASSWORD` ile giriş yap.
   Bu hesap, hiç yönetici yokken admin-api ilk açıldığında otomatik oluşturulur (rolü: owner).
   Şifre 12 karakterden kısaysa ya da büyük/küçük harf ve rakam içermiyorsa ilk girişte yeni şifre istenir.
   Hesap oluştuktan sonra env'deki şifreyi değiştirmek mevcut hesabı etkilemez; şifreyi panelden (Hesabım) değiştir.

## Görsel ve video moderasyonu

İki gönderim yolu var:

```bash
# 0) Önerilen: doğrudan R2'ye yükleme (dosya sunucuya uğramaz)
curl -X POST "https://moderation.<domain>/v1/uploads" -H "Authorization: Bearer mk_live_..." \
  -H "Content-Type: application/json" -d '{"content_type": "image/jpeg", "size": 123456}'
#   → upload_url'e PUT edin, sonra: POST /v1/moderate {"type": "image", "upload_id": "..."}

# 1) Dosyayı API'ye yükle (görsel ≤ 20 MB, video ≤ 200 MB ve ≤ 10 dk)
curl -X POST "https://moderation.<domain>/v1/moderate/upload?wait=true" \
  -H "Authorization: Bearer mk_live_..." -F "file=@foto.jpg" -F "content_id=photo_1"

# 2) Herkese açık URL gönder (sunucu indirir)
curl -X POST "https://moderation.<domain>/v1/moderate" -H "Authorization: Bearer mk_live_..." \
  -H "Content-Type: application/json" -d '{"type": "image", "url": "https://cdn.../foto.jpg"}'
```

Analiz hattı (media-worker, tamamen yerel; içerik dışarı gönderilmez):

1. **Tür tespiti:** dosyanın ilk baytlarından yapılır. Uzantı ve Content-Type'a güvenilmez; tanınmayan dosya 415 alır.
2. **Görsel engel listesi:** moderatörün engellediği içerikler perceptual hash ile karşılaştırılır. Yeniden boyutlandırılmış veya sıkıştırılmış kopyalar da yakalanır.
3. **NudeNet (ONNX, CPU), parçalı tarama:** Büyük karelerin tamamına ek olarak örtüşen dört parçası da taranır; karede küçük kalan bir beden (ör. ekran kaydında pencere içindeki fotoğraf) parçada büyütülerek yakalanır. Docker build sırasında daha hassas 640m modeli indirilmeye çalışılır, inmezse pakete gömülü 320n kullanılır; etkin model medya işçisi logunda yazar. Kural: açık cinsel içerik güçlü tespitte engellenir, çıplaklık incelemeye gider, müstehcen içerik sadece kayda geçer.
4. **OCR (Tesseract Türkçe):** görseldeki metin satırları bulunur, okunur ve Türkçe küfür filtresinden geçirilir.
5. **Animasyonlar ve videolar:** GIF/WebP'de 8 kare, videoda en fazla 24 kare örneklenir. Görsel olarak farklı karelerin hepsi OCR'dan geçer; ortadaki tek bir kareye gizlenen içerik de yakalanır.
6. **Saklama:** orijinal dosya işlendikten sonra silinir. Sadece izin verilmeyen içeriğin küçültülmüş, EXIF'i temizlenmiş kareleri kanıt olarak tutulur ve saklama süresi dolunca silinir.

Güvenlik korumaları:

- **SSRF:** Metadata servisi, iç ağ, localhost, `2130706433` gibi IP hileleri, DNS rebinding ve yönlendirmeyle kaçış engellenir.
- **Görsel bombası:** Piksel sınırı var; küçük dosyadan devasa görsel açılamaz.
- **ffmpeg:** Sadece yerel dosya okuyabilir, zaman aşımı ve iş parçacığı sınırıyla çalışır.
- **Yükleme sırası:** Anahtarsız yükleme, gövde okunmadan reddedilir.
- **Kaynak sınırı:** Medya işçisi ayrı kuyrukta, sınırlı CPU ve bellekle çalışır.

## Proje policy'si

Proje → Policy sekmesinde her kategori için davranış seçilir:

- **Eşiklere göre:** Varsayılan davranış; platform eşikleri geçerli.
- **Yok say:** Kategori bu projede tamamen göz ardı edilir.
- **En fazla incele:** İçerik otomatik engellenmez, en fazla incelemeye gider.
- **Sıkı: engelle:** İnceleme eşiğini geçtiği anda engellenir.

Kategori bazında eşikler de ayarlanabilir. Örneğin çocuklara yönelik bir projede "Taciz" ve "Müstehcen" için sıkı engel seçilebilir, bir flört uygulamasında "Müstehcen" yok sayılabilir. Karar sürümü `v<platform>.p<proje revizyonu>` olarak saklanır. Görsel engel listesi eşleşmeleri her projede her zaman engellenir.

## Layer 2: OpenAI ve maliyet yönetimi

Env'de sadece `AI_API_KEY` bulunur. Geri kalan her şey panelin **AI ve maliyet** sayfasından yönetilir:

- **Sağlayıcı:**
  - OpenAI Moderation API: ücretsiz; metin ve görsel.
  - Sohbet modeli: token başına ücretli. Varsayılan `gpt-5-nano`; silah, uyuşturucu ve spam da puanlanır.
- **Bütçeler:** Günlük, aylık ve proje bazlı (Proje → Policy). Tahmini maliyet çağrıdan **önce** veritabanı kilidi altında ayrılır, çağrıdan sonra gerçek token sayısıyla düzeltilir. Eşzamanlı çağrılar bile bütçeyi aşamaz (testli). Bütçe 0 ise ücretli çağrı yapılmaz; ücretsiz Moderation API çalışmaya devam eder.
- **Fiyat tablosu:** USD / 1M token. Fiyatı tanımlı olmayan ücretli modelle çağrı yapılmaz; maliyeti bilinmeyen harcama olmaz.
- **Dakikalık çağrı sınırı:** OpenAI hız sınırına ve ani maliyet artışına karşı sigorta.
- **Devre kesici:** Art arda 5 hatada 1 dakika, anahtar reddinde (401) 5 dakika AI denenmez. İstekler boşuna beklemez; panelden sıfırlanabilir.
- **Acil durdurma:** Tek anahtarla tüm AI çağrıları kapanır.
- **Uyarılar:** Bütçe eşiği (varsayılan %80), bütçe bitti, anahtar reddi ve fiyatsız model durumlarında panelin üstünde şerit çıkar ve denetim kaydına yazılır.
- **Kayıt ve raporlar:** Her çağrının modeli, token sayısı, maliyeti, süresi ve hatası kaydedilir (30 gün). Günlük maliyet grafiği ve proje/model kırılımı var.
- **Test düğmesi:** Anahtarı ve modeli gerçek bir çağrıyla dener.

Proje → Policy sekmesinde AI modu seçilir:

- **Kapalı:** Sadece yerel modeller karar verir.
- **Akıllı:** Sadece yerel modelin "incele" dediği içerik gönderilir.
- **Her zaman:** Her içerik gönderilir. Yerel modelin zaten engellediği içerik gönderilmez.

Hangi durumda olursa olsun AI yanıt veremezse yerel modelin kararı geçerli olur; içerik asla sessizce onaylanmaz.

- **Gizlilik:** Yerel modelin çıplaklık bulduğu görseller OpenAI'a gönderilmez (OpenAI çocuk güvenliği kuralı).
- **Reşit olmayanlar:** Bu tür cinsel içerik hiçbir proje ayarıyla gevşetilemez.
- **KVKK:** OpenAI'a içerik göndermek yurt dışına veri aktarımıdır; gizlilik politikanızda belirtin.

## Karar kalitesi

"İncele"ye düşen içeriklerde moderatörlerin ne karar verdiği kategori bazında gösterilir (spesifikasyon madde 46: eşikler veriyle belirlenmeli):

- Neredeyse hepsi engelleniyorsa engelleme eşiği düşürülebilir.
- Neredeyse hepsine izin veriliyorsa inceleme eşiği yükseltilebilir.

## KVKK: silme API'si

```bash
# Tek bir içeriğin verisini sil (metin, URL, metadata, IP, kanıt görselleri). Karar kaydı anonim kalır.
curl -X DELETE https://moderation.<domain>/v1/moderate/mod_... -H "Authorization: Bearer mk_live_..."
# Unutulma hakkı: bir kullanıcının bu projedeki tüm verisi (kullanıcı hesabını kapattığında çağırın)
curl -X DELETE https://moderation.<domain>/v1/users/123/data -H "Authorization: Bearer mk_live_..."
```

Ayrıca saklama süresi (Platform ayarları) dolan içerikler otomatik silinir. Silme işlemleri denetim kaydına yazılır; kullanıcı kimliği yazılmaz.

## Yedekleme

`backup` servisi her gün `pg_dump` alır ve son `BACKUP_KEEP_DAYS` günü `backups` volume'unda saklar. Yedek aynı sunucuda durur. Disk arızasına karşı bu klasörü düzenli olarak sunucu dışına kopyalayın (ör. Dokploy'un S3 yedeği veya `rclone`).

Geri yükleme:

```bash
pg_restore -h postgres -U $POSTGRES_USER -d $POSTGRES_DB --clean /backups/db-YYYYMMDD-HHMM.dump
```

## Karar modları

Proje → Policy → Karar akışı bölümünde seçilir:

- **İki adım (varsayılan):** Uygulamaya sadece `allow` veya `block` döner. Risk skoru belirsiz bölgede olan içerik (inceleme eşiği ile engelleme eşiği arası) AI'ya sorulur. AI kapalıysa, bütçe dolduysa veya yanıt veremezse "belirsiz içerik" kuralı uygulanır (varsayılan: engelle; asla sessizce onaylanmaz).
- **Üç adım:** `review` da döner; belirsiz içerik inceleme kuyruğuna düşer ve moderatör karar verir.

**AI tetikleme eşiği:** "Risk eşiğini geçince" modunda, risk skoru eşiği (varsayılan 0,50 = %50) geçen ve yerel modelin kesin engellemediği içerik AI'ya gider. Böylece AI her içeriğe değil, sadece riskli olanlara bakar.

## Yanıt sözleşmesi ve dayanıklılık

Her yanıt JSON'dur ve şu alanları taşır:

- `status`: `completed`, `queued`, `processing`, `failed` veya `unprocessed`
- `decision`: karar ya da `null`
- `reason`: karar gerekçesi
- `retryable`: `failed` ve `unprocessed` durumlarında, isteğin tekrar gönderilip gönderilemeyeceği
- `fallback_decision`: işlenemediğinde projenin önerdiği karar (ayarlanmışsa)

| Arıza | Davranış |
|---|---|
| Redis veya kuyruk yok | Metin API tarafından anında işlenir ve kaydedilir |
| İşçi yok veya yavaş (`wait=true`) | Süre dolunca metni API işler. Atomik sahiplenme sayesinde iki kez işlenmez |
| Veritabanı yok | Son 10 dakikada kullanılmış anahtarlar tanınır; metin yerel modelle değerlendirilir (`degraded: true`, `persisted: false`). Medya: 503 + `unprocessed` |
| Her şey kapalı / süre aşımı | 503 + `{"status": "unprocessed", "retryable": true}`. Hiçbir istek 25 saniyeden uzun sürmez |
| Sunucuya hiç ulaşılamıyor | İstemci tarafında ele alınır: Entegrasyon → Hata yönetimi örneği |

Normal çalışmada her istekte veritabanına bakılır; iptal edilen anahtar ve durdurulan proje anında etkili olur. Önbellek sadece kesinti anında yedek olarak kullanılır.

## Kapasite

Varsayılanlar 8 vCPU / 32 GB sunucu için ayarlıdır:

- **API:** 4 işçi, işçi başına 15 veritabanı bağlantısı.
- **Metin işçisi:** Aynı anda 100 iş.
- **PostgreSQL:** 200 bağlantı, bellek ayarları yapılmış.
- **Hız sınırı:** Proje başına varsayılan saniyede 100, dakikada 3000 istek.

Yük testi: 20 proje, 100 eşzamanlı bağlantı, 3000 istek; 1 CPU'lu test ortamında hepsi işlendi (0 hata, 0 kayıp).

Sunucuda kendi yük testinizi çalıştırmak için api container terminalinde:

```bash
cd /srv && python scripts/loadtest.py --projects 20 --concurrency 100 --requests 3000 --cleanup
```

Uzun süreli çalışma için:

- **Loglar:** Servis başına 50 MB ile sınırlı; disk dolmaz.
- **Redis:** `appendonly` ve `noeviction` ayarlı; kuyruk kaybolmaz.
- **Deploy:** İşteki istekler tamamlanana kadar beklenir (`stop_grace_period`).
- **Bağlantı süresi:** uvicorn keep-alive süresi (95 sn) Traefik'in boşta bağlantı süresinden uzun; ara sıra 502 oluşmaz.

## Akıllı IP koruması

Platform ayarları → Akıllı IP koruması. Modlar:

- **Kapalı:** Otomatik koruma çalışmaz; elle eklenen IP engelleri geçerli kalır.
- **Sadece izle:** Kurallar çalışır ve olaylar kaydedilir, ama kimse banlanmaz. Eşikleri denemek için.
- **Otomatik banla (varsayılan):** Eşiği aşan IP banlanır.

| Kural | Varsayılan eşik | Ne yakalar |
|---|---|---|
| Geçersiz anahtar | 5 dk'da 50 | API anahtarı tahmini |
| Tarama | 5 dk'da 30 adet 404/405 | `/wp-admin`, `/.env` arayan botlar |
| İstek hacmi | 1 dk'da 6000 | Olağandışı trafik seli |
| Hatalı panel girişi | 15 dk'da 20 | Yönetici hesabına kaba kuvvet |

Nasıl çalışır:

- **Kademeli süre:** Aynı IP son 30 günde tekrar banlanırsa süre uzar: 15 dk → 1 sa → 1 gün → 7 gün.
- **Devam eden saldırı:** Ban süresi dolan veya elle kaldırılan IP saldırıya devam ederse yeniden banlanır.
- **Muafiyet:** İç ağ adresleri ve izin listesindeki IP'ler (kendi sunucularınız) asla banlanmaz.
- **Hızlı ret:** Banlı IP'ler Redis üzerinden veritabanına gitmeden reddedilir; hem API'de hem panelde.
- **Yönetim:** Otomatik banlar IP engelleri listesinde "Otomatik" etiketiyle görünür, tek tıkla kaldırılır ve denetim kaydına yazılır.

## Güvenlik denetimi

Son sürümde yapılan kontroller:

- **Bağımlılıklar:** `pip-audit` (Python) ve `npm audit` (panel) ile tarandı; bilinen açık yok.
- **Statik analiz:** `bandit` çalıştırıldı. İşaretlenen SQL kalıplarının tamamı sabit metin veya sabit listeden kolon adı; kullanıcı verisi her zaman parametre olarak gider.
- **Test kapsamı:** SSRF, IP sahteciliği, gövde sınırı, kiracı izolasyonu, CSRF, roller, 2FA tekrar oynatma, hesap kilitleme, otomatik ban ve arıza senaryoları otomatik testlerde.
- **Swagger:** `/docs` varsayılan olarak kapalı (`API_DOCS_ENABLED=true` ile açılır).

Denetimi tekrarlamak için:

```bash
pip install bandit pip-audit
bandit -r app
pip-audit -r requirements.txt -r requirements-media.txt
cd ../panel && npm audit --omit=dev
```

## Medya depolama: Cloudflare R2

**Tüm medya Cloudflare R2'de tutulur. Sunucunun diskine medya yazılmaz; medya için volume da yoktur.**

| Ne | Nerede / ne kadar |
|---|---|
| Doğrudan yükleme (`POST /v1/uploads`) | İstemci imzalı adresle doğrudan R2'ye yükler; baytlar sunucuya hiç uğramaz |
| `POST /v1/moderate/upload` | Gövde diske yazılmadan 8 MB'lık parçalarla R2'ye akıtılır |
| URL ile gönderilen medya | SSRF korumalı indirici doğrudan R2'ye akıtır |
| Analiz | Görsel R2'den belleğe okunur; video ffmpeg ile doğrudan R2'den (imzalı adres) okunur. Kareler RAM'deki `/tmp`'de (tmpfs) |
| Orijinal dosya | Analiz biter bitmez R2'den silinir |
| İzin verilen içerik | Hiçbir şey saklanmaz |
| İnceleme bekleyen içeriğin kanıt kareleri | R2'de, moderatör karar verene kadar |
| Engellenen içeriğin kanıt kareleri | R2'de, `evidence_retention_hours` (varsayılan 72 saat; 0 = hiç) |
| Görsel engel listesi önizlemesi | R2'de, listeden kaldırılana kadar |

**Güvenlik:**

- **Bucket:** Herkese kapalıdır.
- **Panel görüntüleme:** Kanıt kareleri yetkili moderatöre panel üzerinden aktarılır.
- **İmzalı adresler:** 15 dakika geçerlidir ve projeye özeldir; başka proje başkasının yüklemesini kullanamaz.
- **Şifreleme:** R2 veriyi diskte şifreli tutar.

**Güvenlik ağı:** Açılışta bucket'a otomatik silme kuralları kurulur: `incoming/` 1 günde, `evidence/` en geç 35 günde silinir, yarım kalan yüklemeler 1 günde temizlenir. API token'ının bu yetkisi yoksa Cloudflare panelinden elle ekleyin. Uygulama temizliği 15 dakikada bir çalışır; kullanılmayan yüklemeleri 1 saatte siler.

**R2 erişilemezse:** Görsel/video istekleri 503 + `retryable: true` alır. Metin moderasyonu etkilenmez.

### R2 sorun giderme

Sunucuda (Dokploy → api servisi → Terminal):

```bash
cd /srv && python -m app.cli r2-check
```

Komut R2 ayarlarını gösterir; bucket erişimi, yazma, okuma ve silme adımlarını tek tek dener. Hata olursa gerçek R2 kodunu (`NoSuchBucket`, `AccessDenied`, `InvalidAccessKeyId`…) ve Türkçe çözümünü yazar. Aynı kodlar worker loglarında ve panelde (karar ayrıntısı, Sistem durumu) de görünür. Ayar hatalarında iş 5 kez boşuna denenmez, hemen net bir hatayla sonlanır.

**Önemli:** Bucket'a custom domain veya r2.dev bağlamayın; bu bucket'ı herkese açık yapar. Sistem R2'ye özel S3 adresinden, anahtarla bağlanır.

### Cloudflare proxy (turuncu bulut)

Domain Cloudflare üzerinden geçiyorsa gerçek ziyaretçi IP'si `CF-Connecting-IP` başlığından okunur (`TRUST_CLOUDFLARE=true`, varsayılan). Bu başlığa sadece bağlantı Cloudflare'in resmi IP aralığından geliyorsa güvenilir; sahte başlık yok sayılır. IP banı, hız sınırı ve akıllı IP koruması gerçek ziyaretçi IP'sine uygulanır.

### R2 kurulumu

1. Cloudflare → **R2** → **Create bucket** (ör. `moderation-media`). KVKK için AB veri yerleşimi istiyorsanız jurisdiction olarak **EU** seçin ve env'e `R2_JURISDICTION=eu` yazın. Public access **kapalı** kalsın.
2. R2 → **Manage R2 API Tokens** → **Create API token** → izin: **Object Read & Write**, kapsam: **sadece bu bucket**.
3. Env: `R2_ACCOUNT_ID` (R2 sayfasında yazar), `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`.
4. Deploy sonrası panel → Sistem durumu → "Cloudflare R2" kartı yeşil olmalı.

**Maliyet:** Depolama GB başına ayda 0,015 $. Yazma işlemleri milyonda 4,50 $, okuma işlemleri milyonda 0,36 $. Silme ve veri çıkışı ücretsiz. Aylık ücretsiz katman: 10 GB, 1 milyon yazma, 10 milyon okuma. Her görsel yaklaşık 1 yazma + 1 okuma + ücretsiz silme demektir; ayda 100 bin görsel yaklaşık 0,5 $ eder.

## Kurallar, etiketler ve yasal saklama

**Küfür seviyesi** (Proje → Policy):

- **Sıkı:** Ağır küfür ve sokak ağzı (amk, mk, aq) engellenir; hafif hakaret (salak, aptal) belirsiz sayılır.
- **Orta:** Sadece ağır küfür engellenir.
- **Kapalı:** Küfür filtresi çalışmaz.

**Yasaklı kelimeler** (Kurallar sayfası):

- Tüm platform veya tek proje için eklenir; her kelime bir etiket ve önem (Engelle / Kritik) taşır.
- Metinde ve görsel/videodaki yazıda (OCR) aranır.
- Gizleme denemeleri de yakalanır: `P.K.K`, `p k k'lılar`.
- "Tam kelime" modu kısa kelimelerde yanlış eşleşmeyi önler: `apo` ≠ `apollo`.

**Yasaklı görseller** (Kurallar sayfası):

- Tarif ettiğiniz sembol, bayrak, amblem ve yazılar her görsel ve videoda AI görsel kontrolüyle aranır (sohbet modeli, bütçeden düşer).
- Varsayılan kurallar: PKK/KCK/PYD/YPG ve IŞİD sembolleri.
- Kişiler yüzünden tanınmaz; isim ve sembollerle yakalanır.

**Yanıt:** Her karar `labels` (ör. `["terör:pkk"]`, `["küfür"]`) ve `severity` (`normal` / `critical`) taşır. Webhook'ta da aynı alanlar gelir. `critical` şu durumlarda döner:

- pornografi,
- çocuk istismarı,
- terör,
- "Kritik" işaretli kurallar.

Uygulamanız bu durumda kullanıcıya işlem yapabilir (ör. hesabı kapatma).

**Yasal saklama** (Ayarlar'dan açılıp kapatılır, varsayılan açık):

- **Ne saklanır:** Kritik içeriğin orijinal medyası (R2 `hold/`), metni, IP'si ve kullanıcı bilgileri yönetici silene kadar saklanır.
- **Ne dokunamaz:** Otomatik temizlik, saklama süresi ve uygulamanın silme API'si bu kayıtlara dokunmaz.
- **Proje silme:** Yasal saklamada kayıt varsa proje silinemez.
- **Panel:** Yasal saklama sayfasında listelenir. Orijinal indirme ve kalıcı silme (kimlik yazarak onay) denetim kaydına yazılır.
- **Çocuk istismarı:** Bu şüphe taşıyan içerik panelde gösterilmez ve indirilemez; yetkililere bildirilmelidir.

**Kullanıcı bilgileri:** İsteğe `user: {id, name, surname, username, email, phone, extra}` eklenebilir (isteğe bağlı; multipart'ta `user` alanında JSON).

- AES-GCM ile şifreli saklanır.
- Yanıtta dönmez.
- Panelde sadece admin/owner görür.
- Saklama süresiyle silinir (yasal saklama hariç).

## Webhook

Proje → Webhook sekmesinden bir https adresi girin. Olaylar:

- `moderation.completed`: karar hazır
- `moderation.reviewed`: moderatör karar verdi
- `moderation.failed`: içerik işlenemedi

Her istekte `X-Moderation-Signature: t=<unix>,v1=<HMAC-SHA256(secret, "t.gövde")>` başlığı gelir. Entegrasyon sekmesinde doğrulama örneği var. 2xx dışındaki yanıtlarda 8 kez, yaklaşık 1 gün boyunca tekrar denenir. Olaylar önce veritabanına yazılır; kuyruk kaybolsa bile teslimat kaybolmaz.

## Panele giriş yapılamıyorsa

Env'deki `ADMIN_PASSWORD` sadece hesap **ilk oluşturulurken** kullanılır. İlk girişte şifre değiştirildiyse geçerli şifre, panelde belirlenen yeni şifredir. Çözüm yolları:

1. **Teşhis:** Dokploy'da admin-api loglarına bakın. Her açılışta yöneticilerin durumu yazılır (`Panel yöneticisi: e-posta [owner, active, KİLİTLİ …]`), her başarısız girişin sebebi de loglanır (`wrong_password`, `user_not_found`, `account_locked`). Aynı bilgi api terminalinde de alınabilir:

   ```bash
   cd /srv && python -m app.cli admin-status
   ```

2. **Env ile sıfırlama:** Env'e `ADMIN_RESET_PASSWORD=true` ekleyip deploy edin. `ADMIN_EMAIL` hesabının şifresi `ADMIN_PASSWORD` olur, kilit açılır; hesap yoksa oluşturulur. Aynı env şifresiyle yalnızca bir kez uygulanır; sonra panelde değiştirdiğiniz şifre ezilmez. Giriş yaptıktan sonra satırı `false` yapın.

3. **Terminal ile sıfırlama:**

   ```bash
   cd /srv && python -m app.cli reset-admin-password --email sen@alanadi.com
   ```

4. **IP'niz banlandıysa** (çok sayıda hatalı deneme):

   ```bash
   cd /srv && python -m app.cli unban-ip --ip SİZİN.IP.ADRESİNİZ
   ```

## Admin paneli

- **Genel bakış:** istek sayıları, karar dağılımı, zaman serisi, yanıt süresi (P50/P95/P99), kategoriler, proje bazında trafik.
- **İnceleme kuyruğu:** otomatik sistemin emin olamadığı içeriklere izin ver / engelle. Karar, `GET /v1/moderate/{id}` yanıtına `reason: human_reviewed` olarak yansır.
- **Kararlar:** tüm istekler; proje, karar, durum ve ID ile filtreleme, ayrıntı görünümü.
- **Projeler:** oluştur, düzenle, durdur/başlat (durdurulan proje 403 alır), hız sınırı, sil (slug yazarak onay).
- **API anahtarları:** oluştur (tek seferlik gösterilir), yenile (eski anahtara geçiş süresi tanıyarak), durdur, iptal et, anahtarın kullanıldığı IP'ler.
- **IP engelleri:** platform geneli veya tek proje için; tek IP veya CIDR; süreli veya süresiz.
- **Platform ayarları:** hizmeti aç/kapat (kapalıyken API 503 döner), karar eşikleri (her değişiklik yeni policy sürümü), varsayılan hız sınırı.
- **Yöneticiler:** kullanıcı adıyla ekle (e-posta isteğe bağlı), rol değiştir, durdur, şifre sıfırla, sil.
- **Tema:** açık / koyu mod (sol alttaki düğme, tercih tarayıcıda saklanır). İkonlar Hugeicons (ücretsiz set).
- **Denetim kaydı:** panelde yapılan her değişiklik kim/ne zaman/hangi IP bilgisiyle.
- **Sistem durumu:** veritabanı, Redis, worker, kuyruk.

Panel ayar değişiklikleri API ve worker'a en fazla birkaç saniyede yansır; yeniden deploy gerekmez.

### Roller

| Rol | Yetki |
|---|---|
| Sahip (owner) | Her şey + yönetici ekleme/çıkarma |
| Yönetici (admin) | Projeler, anahtarlar, IP engelleri, ayarlar, denetim kaydı |
| Moderatör | İnceleme kuyruğu, kararlar, istatistikler |
| İzleyici | Sadece görüntüleme (anahtarları göremez) |

### Güvenlik

- Şifreler Argon2id ile saklanır; en az 12 karakter, büyük/küçük harf ve rakam zorunlu.
- Giriş kullanıcı adıyla (veya tanımlıysa e-postayla) yapılır.
- İki adımlı doğrulama (TOTP, Google Authenticator/1Password uyumlu) ve tek kullanımlık kurtarma kodları.
  Aynı kod iki kez kullanılamaz. Sır veritabanında AES-GCM ile şifreli durur. Owner, telefonunu kaybeden yöneticinin 2FA'sını sıfırlayabilir.
- 5 hatalı girişte hesap 15 dakika kilitlenir; IP başına 15 dakikada 20 deneme sınırı.
- Oturum sunucu tarafında tutulur (HttpOnly, Secure, SameSite=Strict cookie). Mutlak ömür 12 saat, 2 saat işlem yapılmazsa düşer.
- Rol değişince, hesap durdurulunca veya şifre sıfırlanınca kişinin açık oturumları kapanır.
- CSRF koruması: özel başlık zorunlu, CORS kapalı.
- nginx: sıkı CSP, `X-Frame-Options: DENY`, HSTS, `noindex`.
- Son owner silinemez veya düşürülemez; kimse kendi rolünü düşüremez.

## Komut satırı (api container terminalinden, `/srv` içinde)

```bash
python -m app.cli create-admin --email ahmet@alanadi.com --role admin
python -m app.cli reset-admin-password --email sen@alanadi.com   # şifreyi unutursan / hesap kilitlenirse
python -m app.cli create-project --name KAYS --slug kays --env test
python -m app.cli create-key --project kays --name "KAYS Production" --env live
python -m app.cli list-keys --project kays
python -m app.cli revoke-key --id <key-uuid>
```

Panel varken bunlara normalde gerek yok; panel erişimini kaybedersen kurtarma için kullanılır.
`reset-admin-password` geçici bir şifre üretir, kilidi açar ve hesabı tekrar aktif eder.

## Testler

114 entegrasyon testi var (küfür seviyeleri, yasaklı kelime ve görseller, yasal saklama, parçalı çıplaklık taraması, Retina ekran kaydındaki küçük yazı, Cloudflare gerçek IP, R2 hata teşhisi, Cloudflare R2 depolama, doğrudan yükleme ve 8 MB'lık parçalarla akış, medya depolama ve disk koruması, panel girişi kurtarma senaryoları, akıllı IP koruması, e-postayla giriş, iki/üç adımlı karar, Redis/işçi/veritabanı kesintisi ve zaman aşımı senaryoları, AI bütçesi ve eşzamanlı bütçe aşımı, devre kesici, proje policy'si, Layer 2 akıllı/her zaman modları ve AI hata güvenliği, KVKK silme API'si dahil). Kapsamı: SSRF, IP sahteciliği, gövde sınırı, kiracı izolasyonu, idempotency, görsel/video kararları, gizli GIF karesi, video kare zamanı, engel listesi, imzalı webhook, 2FA (tekrar oynatma dahil), roller, hesap kilitleme ve KVKK temizliği. Çalışan bir stack'e karşı koşar:

```bash
cd backend
pip install -r requirements.txt -r requirements-media.txt -r requirements-dev.txt
# Yerel S3 (R2 yerine): pip install "moto[server]" && moto_server -p 9000 && curl -X PUT http://127.0.0.1:9000/moderation-test
# Servisleri R2_ENDPOINT=http://127.0.0.1:9000 R2_ACCESS_KEY_ID=test R2_SECRET_ACCESS_KEY=test R2_BUCKET=moderation-test ile başlatın
# api, admin-api, worker, media-worker ALLOW_PRIVATE_NETWORK=true FORWARDED_ALLOW_IPS=127.0.0.1 ile çalışırken:
# Layer 2 testleri için stack'i ayrıca AI_BASE_URL=http://127.0.0.1:18999/v1 ile başlatın (testler sahte model sunar)
ALLOW_PRIVATE_NETWORK=true FORWARDED_ALLOW_IPS=127.0.0.1 AI_BASE_URL=http://127.0.0.1:18999/v1 pytest -q tests
```

`ALLOW_PRIVATE_NETWORK` sadece testte yerel webhook alıcısı için vardır. Production'da asla açmayın.

## Lokal geliştirme

```bash
cp .env.example .env   # değerleri doldur
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
# API:   http://localhost:8000
# Panel: http://localhost:8080
```

Sadece paneli geliştirmek için (admin API `localhost:8001`'de çalışırken):

```bash
cd panel && npm install && npm run dev   # http://localhost:5173
```

## API

| Endpoint | Açıklama |
|---|---|
| `POST /v1/uploads` | Doğrudan R2 yükleme adresi al (görsel/video). |
| `POST /v1/moderate` | İçerik gönder (metin, `url` veya `upload_id`). `?wait=true` ile sonucu kısa süre bekler. |
| `POST /v1/moderate/upload` | Dosyayı multipart ile gönder (R2'ye akıtılır). |
| `GET /v1/moderate/{id}` | Sonucu sorgula (insan incelemesi yapıldıysa onun kararı döner). |
| `GET /health`, `GET /ready` | Sağlık kontrolü. |
| `GET /docs` | Swagger (varsayılan kapalı; `API_DOCS_ENABLED=true`). |

Başlıklar: `Authorization: Bearer mk_...`, isteğe bağlı `Idempotency-Key`.

Hata kodları: `401` geçersiz key, `403` proje durdurulmuş (`project_disabled`) veya IP engelli (`ip_blocked`), `409` aynı Idempotency-Key farklı body ile, `413` istek çok büyük, `422` geçersiz istek, `429` rate limit (`Retry-After` ile), `503` hizmet panelden kapatılmış.

## Yapı

```
docker-compose.yml          Dokploy için (dışarı port açmaz)
docker-compose.local.yml    Lokal: API 8000, panel 8080
backend/
  start-api.sh              migration + uvicorn
  migrations/               SQL migration'lar (sırayla, bir kez uygulanır)
  app/
    api/                    public API: auth, IP/hizmet kontrolü, rate limit, endpoint'ler
    admin/                  admin API: oturum, roller, audit ve panel endpoint'leri
    worker/                 arq worker + takılı iş kurtarma cron'u
    moderation/             normalizasyon, text pipeline, policy
    platform_settings.py    panelden yönetilen ayarlar (önbellekli)
panel/
  Dockerfile, nginx.conf    build + statik servis + /api → admin-api
  src/pages/                panel sayfaları
```

## Bilinen eksikler

- `content_text` süresiz saklanıyor → saklama süresi ve temizleme job'ı (KVKK)
- Kelime listesi küçük → gerçek veriyle büyütülecek
- Şiddet/silah/kan tespiti yok (NudeNet sadece çıplaklık). Sonraki adım: CLIP tabanlı sıfır-atış sınıflandırıcı
- Videoda ses (konuşma) analizi yok. Sonraki adım: Whisper ile konuşmayı metne çevirip metin filtresinden geçirmek
- CSAM için yasal hash veritabanları (PhotoDNA / Thorn Safer) başvuru gerektirir; açık çıplaklık zaten engellenir
- Metin tarafında kelime listesi var, anlam anlayan Türkçe model (BERTurk) yok
- Postgres Row Level Security ve tablo partitioning (şu an uygulama seviyesinde project_id izolasyonu var, testli)
- Presigned upload (şu an doğrudan yükleme + URL var)
- Organizasyon, faturalama ve SSO (bilinçli olarak ertelendi)
- Panel için IP allowlist
- Postgres yedeklemesi → Dokploy'un backup özelliğiyle veya harici olarak ayarlanmalı

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Add01Icon, ArrowLeft01Icon, Key01Icon } from "@hugeicons/core-free-icons";
import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { api, ApiError, del, patch, post } from "../api";
import { useAuth } from "../auth";
import { OverviewView, RANGE_OPTIONS } from "../components/Overview";
import { Icon } from "../components/icon";
import { useToast } from "../components/toast";
import {
  Button, CopyButton, Empty, ErrorBox, Field, Input, Modal, Mono, Panel, SecretReveal, Segmented, Select, Spinner,
  Switch, Table, Tabs, Tag, Textarea,
} from "../components/ui";
import { can, dateTime, num, relative } from "../format";
import type { ApiKey, PlatformSettings, Project, RangeKey } from "../types";
import { IpRulesManager } from "./IpRules";
import { PolicyTab } from "./ProjectPolicy";
import { WebhookTab } from "./ProjectWebhook";

const PUBLIC_API_URL = (import.meta.env.VITE_PUBLIC_API_URL as string | undefined) || "https://moderation.example.com";

// ---------------------------------------------------------------- API keys

function KeyStatus({ k }: { k: ApiKey }) {
  const expired = k.expires_at && new Date(k.expires_at) < new Date();
  if (k.status === "revoked") return <Tag tone="bad">İptal edildi</Tag>;
  if (k.status === "disabled") return <Tag tone="warn">Devre dışı</Tag>;
  if (expired) return <Tag>Süresi doldu</Tag>;
  return <Tag tone="good">Aktif</Tag>;
}

function CreateKeyModal({ projectId, open, onClose }: { projectId: string; open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [env, setEnv] = useState<"live" | "test">("live");
  const [days, setDays] = useState("");
  const [key, setKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const create = useMutation({
    mutationFn: () => post<{ api_key: string }>(`/projects/${projectId}/keys`, {
      name, environment: env, expires_in_days: days ? Number(days) : null,
    }),
    onSuccess: (r) => { setKey(r.api_key); qc.invalidateQueries({ queryKey: ["keys", projectId] }); qc.invalidateQueries({ queryKey: ["projects"] }); },
    onError: (e) => setError((e as ApiError).message),
  });
  const close = () => { setName(""); setEnv("live"); setDays(""); setKey(null); setError(null); onClose(); };

  if (key) {
    return (
      <Modal open={open} onClose={close} title="Anahtar oluşturuldu" footer={<Button variant="primary" onClick={close}>Kaydettim</Button>}>
        <SecretReveal value={key} warning="Bu anahtar sadece şimdi gösteriliyor. Güvenli bir yere (ör. uygulamanızın ortam değişkenlerine) kaydedin." />
      </Modal>
    );
  }
  return (
    <Modal open={open} onClose={close} title="Yeni API anahtarı"
      description="Ortam başına ayrı anahtar kullanın; böylece birini iptal ettiğinizde diğerleri etkilenmez."
      footer={<><Button onClick={close}>Vazgeç</Button><Button variant="primary" type="submit" form="create-key" loading={create.isPending}>Anahtar oluştur</Button></>}>
      <form id="create-key" className="space-y-4" onSubmit={(e) => { e.preventDefault(); setError(null); create.mutate(); }}>
        {error && <div role="alert" className="rounded-md bg-block-soft px-3 py-2 text-sm text-block-ink">{error}</div>}
        <Field label="Anahtar adı" hint="Nerede kullanıldığını anlatan bir isim: Production, Staging, Geliştirici makinesi…">
          {(id) => <Input id={id} required maxLength={100} value={name} onChange={(e) => setName(e.target.value)} />}
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Ortam">
            {(id) => (
              <Select id={id} value={env} onChange={(e) => setEnv(e.target.value as "live" | "test")}>
                <option value="live">Canlı (mk_live_)</option>
                <option value="test">Test (mk_test_)</option>
              </Select>
            )}
          </Field>
          <Field label="Geçerlilik">
            {(id) => (
              <Select id={id} value={days} onChange={(e) => setDays(e.target.value)}>
                <option value="">Süresiz</option>
                <option value="30">30 gün</option>
                <option value="90">90 gün</option>
                <option value="365">1 yıl</option>
              </Select>
            )}
          </Field>
        </div>
      </form>
    </Modal>
  );
}

type KeyAction = { key: ApiKey; kind: "rotate" | "disable" | "enable" | "revoke" };

function KeysTab({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [creating, setCreating] = useState(false);
  const [action, setAction] = useState<KeyAction | null>(null);
  const [grace, setGrace] = useState("0");
  const [rotated, setRotated] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const q = useQuery({ queryKey: ["keys", projectId], queryFn: () => api<ApiKey[]>(`/projects/${projectId}/keys`) });

  const run = useMutation({
    mutationFn: async ({ key, kind }: KeyAction) =>
      kind === "rotate" ? post<{ api_key: string }>(`/keys/${key.id}/rotate`, { grace_hours: Number(grace) }) : post(`/keys/${key.id}/${kind}`),
    onSuccess: (r, { kind, key }) => {
      qc.invalidateQueries({ queryKey: ["keys", projectId] });
      setAction(null);
      if (kind === "rotate") setRotated((r as { api_key: string }).api_key);
      else toast("success", { disable: `${key.name} devre dışı bırakıldı.`, enable: `${key.name} tekrar etkin.`, revoke: `${key.name} iptal edildi.` }[kind]);
    },
    onError: (e) => toast("error", (e as Error).message),
  });

  const copy: Record<KeyAction["kind"], { title: string; text: string; button: string; variant: "primary" | "danger" }> = {
    rotate: { title: "Anahtarı yenile", text: "Aynı ada sahip yeni bir anahtar oluşturulur. Eski anahtarın ne zaman kapanacağını seçin.", button: "Yenile", variant: "primary" },
    disable: { title: "Anahtarı devre dışı bırak", text: "Bu anahtarla gelen istekler reddedilir. Daha sonra tekrar etkinleştirebilirsiniz.", button: "Devre dışı bırak", variant: "danger" },
    enable: { title: "Anahtarı etkinleştir", text: "Bu anahtarla gelen istekler tekrar kabul edilir.", button: "Etkinleştir", variant: "primary" },
    revoke: { title: "Anahtarı iptal et", text: "Bu işlem geri alınamaz. Anahtar kalıcı olarak kullanılamaz hale gelir.", button: "Kalıcı olarak iptal et", variant: "danger" },
  };

  return (
    <>
      <Panel padded={false} title="API anahtarları" description="Anahtarların tamamı saklanmaz; sadece ilk karakterleri gösterilir."
        actions={<Button size="sm" variant="primary" icon={<Icon icon={Add01Icon} size={14} />} onClick={() => setCreating(true)}>Yeni anahtar</Button>}>
        {q.isLoading ? <Spinner /> : q.error ? <div className="p-5"><ErrorBox error={q.error} /></div> : q.data!.length === 0 ? (
          <Empty title="Anahtar yok" />
        ) : (
          <Table>
            <thead><tr><th>Ad</th><th>Anahtar</th><th>Durum</th><th>Son kullanım</th><th>Kullanılan IP'ler</th><th>Oluşturulma</th><th /></tr></thead>
            <tbody>
              {q.data!.map((k) => (
                <tr key={k.id} className={k.status === "revoked" ? "text-muted" : ""}>
                  <td className="font-medium">{k.name}<div className="mt-0.5"><Tag tone={k.environment === "live" ? "info" : "neutral"}>{k.environment === "live" ? "Canlı" : "Test"}</Tag></div></td>
                  <td><Mono>{k.key_prefix}…</Mono></td>
                  <td><KeyStatus k={k} />{k.expires_at && k.status !== "revoked" && <div className="mt-0.5 text-xs text-muted">bitiş {relative(k.expires_at)}</div>}</td>
                  <td title={dateTime(k.last_used_at)}>{relative(k.last_used_at)}</td>
                  <td>
                    {k.recent_ips.length === 0 ? <span className="text-faint">—</span> : (
                      <button className="text-left" onClick={() => setExpanded(expanded === k.id ? null : k.id)}>
                        <Mono>{k.recent_ips[0].ip}</Mono>
                        {k.recent_ips.length > 1 && <span className="ml-1 text-xs text-muted underline">+{k.recent_ips.length - 1}</span>}
                        {expanded === k.id && (
                          <ul className="mt-2 space-y-1 text-xs">
                            {k.recent_ips.map((ip) => <li key={ip.ip}><Mono>{ip.ip}</Mono> <span className="text-muted">{num(ip.requests)} istek · {relative(ip.last_seen)}</span></li>)}
                          </ul>
                        )}
                      </button>
                    )}
                  </td>
                  <td className="text-muted">{dateTime(k.created_at)}<div className="text-xs">{k.created_by_email ?? "komut satırı"}</div></td>
                  <td>
                    {k.status !== "revoked" && (
                      <div className="flex justify-end gap-1">
                        <Button size="sm" variant="ghost" onClick={() => { setGrace("0"); setAction({ key: k, kind: "rotate" }); }}>Yenile</Button>
                        {k.status === "active"
                          ? <Button size="sm" variant="ghost" onClick={() => setAction({ key: k, kind: "disable" })}>Durdur</Button>
                          : <Button size="sm" variant="ghost" onClick={() => setAction({ key: k, kind: "enable" })}>Etkinleştir</Button>}
                        <Button size="sm" variant="ghostDanger" onClick={() => setAction({ key: k, kind: "revoke" })}>İptal et</Button>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Panel>

      <CreateKeyModal projectId={projectId} open={creating} onClose={() => setCreating(false)} />

      <Modal open={!!action} onClose={() => setAction(null)} title={action ? copy[action.kind].title : ""}
        description={action ? <><strong className="font-medium text-ink">{action.key.name}</strong> (<Mono>{action.key.key_prefix}…</Mono>). {copy[action.kind].text}</> : ""}
        footer={action && <><Button onClick={() => setAction(null)}>Vazgeç</Button><Button variant={copy[action.kind].variant} loading={run.isPending} onClick={() => run.mutate(action)}>{copy[action.kind].button}</Button></>}>
        {action?.kind === "rotate" && (
          <Field label="Eski anahtar" hint="Uygulamanızı yeni anahtara geçirmek için süre tanıyabilirsiniz.">
            {(id) => (
              <Select id={id} value={grace} onChange={(e) => setGrace(e.target.value)}>
                <option value="0">Hemen iptal edilsin</option>
                <option value="1">1 saat daha çalışsın</option>
                <option value="24">24 saat daha çalışsın</option>
                <option value="168">7 gün daha çalışsın</option>
              </Select>
            )}
          </Field>
        )}
      </Modal>

      <Modal open={!!rotated} onClose={() => setRotated(null)} title="Yeni anahtar hazır" footer={<Button variant="primary" onClick={() => setRotated(null)}>Kaydettim</Button>}>
        {rotated && <SecretReveal value={rotated} warning="Yeni anahtar sadece şimdi gösteriliyor. Uygulamanızı bu anahtarla güncelleyin." />}
      </Modal>
    </>
  );
}

// ---------------------------------------------------------------- Integration

function IntegrationTab() {
  const [lang, setLang] = useState<"text" | "url" | "direct" | "upload" | "node" | "verify" | "delete" | "errors">("text");
  const examples = {
    text: `curl -X POST "${PUBLIC_API_URL}/v1/moderate?wait=true" \\
  -H "Authorization: Bearer $MODERATION_API_KEY" \\
  -H "Content-Type: application/json" \\
  -H "Idempotency-Key: post_456" \\
  -d '{"type": "text", "text": "Kullanıcı yorumu", "content_id": "post_456",
       "user": {"id": "123", "name": "Ali", "surname": "Veli", "email": "ali@ornek.com", "phone": "+905551112233"}}'
# Yanıt: {"decision": "block", "reason": "custom_term_detected", "labels": ["terör:pkk"], "severity": "critical", ...}
# severity "critical" (pornografi, çocuk istismarı, terör, kritik kural) → kullanıcının hesabını kapatın.
# "user" isteğe bağlıdır; şifreli saklanır, yanıtta dönmez, panelde sadece yöneticiye görünür.`,
    url: `# Görsel veya video herkese açık bir adresteyse (sunucu indirir; iç ağ adresleri reddedilir)
curl -X POST "${PUBLIC_API_URL}/v1/moderate" \\
  -H "Authorization: Bearer $MODERATION_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"type": "image", "url": "https://cdn.uygulamaniz.com/u/123/foto.jpg", "content_id": "photo_789"}'
# type: "video" için aynı. Sonuç webhook ile gelir veya GET /v1/moderate/{id} ile sorgulanır.`,
    direct: `# Önerilen: dosya doğrudan Cloudflare R2'ye yüklenir, bu sunucuya hiç uğramaz (büyük video için ideal)
# 1) Yükleme adresi al
curl -X POST "${PUBLIC_API_URL}/v1/uploads" \\
  -H "Authorization: Bearer $MODERATION_API_KEY" -H "Content-Type: application/json" \\
  -d '{"content_type": "video/mp4", "size": 48213442}'
# → {"upload_id": "…", "upload_url": "https://…r2.cloudflarestorage.com/…", "method": "PUT",
#    "headers": {"Content-Type": "video/mp4"}, "expires_in": 900}

# 2) Dosyayı bu adrese PUT et (15 dakika geçerli)
curl -X PUT "$UPLOAD_URL" -H "Content-Type: video/mp4" --data-binary @video.mp4

# 3) Moderasyona gönder
curl -X POST "${PUBLIC_API_URL}/v1/moderate" \\
  -H "Authorization: Bearer $MODERATION_API_KEY" -H "Content-Type: application/json" \\
  -d '{"type": "video", "upload_id": "…", "content_id": "video_123"}'
# Sonuç webhook ile gelir. Dosya işlendikten hemen sonra R2'den silinir.`,
    upload: `# Dosyayı doğrudan gönder (görsel 20 MB, video 200 MB / 10 dk'ya kadar)
curl -X POST "${PUBLIC_API_URL}/v1/moderate/upload?wait=true" \\
  -H "Authorization: Bearer $MODERATION_API_KEY" \\
  -H "Idempotency-Key: photo_789" \\
  -F "file=@foto.jpg" \\
  -F "user_id=123" \\
  -F "content_id=photo_789"
# Tür dosyanın içeriğinden anlaşılır: JPEG, PNG, GIF, WebP, MP4, MOV, WebM, AVI`,
    node: `const form = new FormData();
form.append("file", new Blob([buffer]), "upload");
form.append("content_id", photo.id);

const res = await fetch("${PUBLIC_API_URL}/v1/moderate/upload?wait=true", {
  method: "POST",
  headers: { Authorization: \`Bearer \${process.env.MODERATION_API_KEY}\`, "Idempotency-Key": photo.id },
  body: form,
});
const result = await res.json();
// result.status === "completed" → result.decision: "allow" | "review" | "block"
// result.status === "queued"    → sonucu webhook ile bekleyin`,
    errors: `// Her durumda bir sonuç kaydedin: sunucuya hiç ulaşılamasa bile
async function moderate(text, contentId) {
  try {
    const res = await fetch("${PUBLIC_API_URL}/v1/moderate?wait=true", {
      method: "POST",
      signal: AbortSignal.timeout(30_000),          // asla sonsuza kadar bekleme
      headers: {
        Authorization: \`Bearer \${process.env.MODERATION_API_KEY}\`,
        "Content-Type": "application/json",
        "Idempotency-Key": contentId,               // tekrar denemede aynı içerik iki kez işlenmez
      },
      body: JSON.stringify({ type: "text", text, content_id: contentId }),
    });
    const body = await res.json().catch(() => null);

    if (body?.status === "completed") return { state: "done", decision: body.decision };   // allow | block (| review)
    if (body?.status === "queued" || body?.status === "processing") {
      return { state: "pending", id: body.id };      // karar webhook ile gelecek
    }
    // unprocessed / failed / 429: sistem cevap verdi ama içeriği işleyemedi
    return { state: "unprocessed", decision: body?.fallback_decision ?? null, retry: body?.retryable ?? true };
  } catch {
    // Sunucuya ulaşılamadı (ağ, DNS, bakım): yine de "işlenmedi" olarak kaydedin, sonra tekrar deneyin
    return { state: "unprocessed", decision: null, retry: true };
  }
}`,
    delete: `# KVKK: tek bir içeriğin verisini sil (metin, görsel kanıtları, IP). Karar kaydı anonim kalır.
curl -X DELETE "${PUBLIC_API_URL}/v1/moderate/mod_..." -H "Authorization: Bearer $MODERATION_API_KEY"

# Unutulma hakkı: bir kullanıcının bu projedeki tüm verisini sil (kullanıcı hesabını kapattığında çağırın)
curl -X DELETE "${PUBLIC_API_URL}/v1/users/123/data" -H "Authorization: Bearer $MODERATION_API_KEY"
# → {"deleted": 42}`,
    verify: `// Webhook imzasını doğrulama (Node.js / Express)
import crypto from "node:crypto";

app.post("/webhooks/moderation", express.raw({ type: "application/json" }), (req, res) => {
  const header = req.get("X-Moderation-Signature") ?? "";
  const parts = Object.fromEntries(header.split(",").map((p) => p.split("=")));
  const expected = crypto.createHmac("sha256", process.env.MODERATION_WEBHOOK_SECRET)
    .update(\`\${parts.t}.\`).update(req.body).digest("hex");
  const fresh = Math.abs(Date.now() / 1000 - Number(parts.t)) < 300;
  if (!fresh || !crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(parts.v1 ?? ""))) {
    return res.sendStatus(401);
  }
  const event = JSON.parse(req.body);   // { event, data: { id, decision, content_id, ... } }
  res.sendStatus(200);                  // hızlı 200 dönün, işi arka planda yapın
});`,
  };
  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
      <Panel title="İstek örneği" actions={<Segmented label="Örnek" value={lang} onChange={setLang} options={[{ value: "text", label: "Metin" }, { value: "url", label: "Görsel URL" }, { value: "direct", label: "Doğrudan yükleme" }, { value: "upload", label: "Dosya" }, { value: "node", label: "Node.js" }, { value: "verify", label: "Webhook" }, { value: "delete", label: "Silme" }, { value: "errors", label: "Hata yönetimi" }]} />}>
        <div className="relative">
          <pre className="overflow-x-auto rounded-lg border border-line bg-sunken p-4 font-mono text-xs leading-relaxed text-ink"><code>{examples[lang]}</code></pre>
          <div className="absolute right-2 top-2"><CopyButton value={examples[lang]} /></div>
        </div>
      </Panel>
      <Panel title="Yanıtlar">
        <dl className="space-y-3 text-sm">
          <div><dt className="font-medium">200 / 202</dt><dd className="text-muted">Karar hazır (<Mono>completed</Mono>) veya işleniyor (<Mono>queued</Mono>). <Mono>degraded: true</Mono> ise veritabanı kesintisinde yerel modelle verilmiş karardır.</dd></div>
          <div><dt className="font-medium">503</dt><dd className="text-muted"><Mono>status: unprocessed</Mono>, <Mono>retryable: true</Mono> ve varsa <Mono>fallback_decision</Mono>: içerik işlenemedi, "işlenmedi" olarak kaydedip sonra tekrar deneyin.</dd></div>
          <div><dt className="font-medium">413 / 415</dt><dd className="text-muted">Dosya çok büyük / desteklenmeyen format.</dd></div>
          <div><dt className="font-medium">401</dt><dd className="text-muted">Anahtar geçersiz, iptal edilmiş veya süresi dolmuş.</dd></div>
          <div><dt className="font-medium">403</dt><dd className="text-muted">Proje durdurulmuş veya IP engellenmiş.</dd></div>
          <div><dt className="font-medium">409</dt><dd className="text-muted">Aynı Idempotency-Key farklı içerikle gönderildi.</dd></div>
          <div><dt className="font-medium">429</dt><dd className="text-muted">Hız sınırı aşıldı; <Mono>Retry-After</Mono> kadar bekleyin.</dd></div>
          <div><dt className="font-medium">503</dt><dd className="text-muted">Hizmet panelden kapatılmış.</dd></div>
        </dl>
      </Panel>
    </div>
  );
}

// ---------------------------------------------------------------- Settings

function SettingsTab({ project }: { project: Project }) {
  const qc = useQueryClient();
  const toast = useToast();
  const navigate = useNavigate();
  const { user } = useAuth();
  const isAdmin = can(user?.role, "admin");
  const platform = useQuery({ queryKey: ["settings"], queryFn: () => api<PlatformSettings>("/settings") });

  const [name, setName] = useState(project.name);
  const [description, setDescription] = useState(project.description ?? "");
  const [custom, setCustom] = useState(project.rate_limit_per_second != null || project.rate_limit_per_minute != null);
  const [rps, setRps] = useState(String(project.rate_limit_per_second ?? ""));
  const [rpm, setRpm] = useState(String(project.rate_limit_per_minute ?? ""));
  const [confirmToggle, setConfirmToggle] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [confirmSlug, setConfirmSlug] = useState("");

  useEffect(() => { setName(project.name); setDescription(project.description ?? ""); }, [project.name, project.description]);

  const refresh = () => { qc.invalidateQueries({ queryKey: ["project", project.id] }); qc.invalidateQueries({ queryKey: ["projects"] }); };
  const save = useMutation({
    mutationFn: (body: object) => patch(`/projects/${project.id}`, body),
    onSuccess: () => { toast("success", "Değişiklikler kaydedildi."); refresh(); },
    onError: (e) => toast("error", (e as Error).message),
  });
  const toggle = useMutation({
    mutationFn: () => patch(`/projects/${project.id}`, { status: project.status === "active" ? "disabled" : "active" }),
    onSuccess: () => { toast("success", project.status === "active" ? "Proje durduruldu." : "Proje tekrar aktif."); refresh(); setConfirmToggle(false); },
    onError: (e) => toast("error", (e as Error).message),
  });
  const remove = useMutation({
    mutationFn: () => del(`/projects/${project.id}`, { confirm_slug: confirmSlug }),
    onSuccess: () => { toast("success", `${project.name} silindi.`); qc.invalidateQueries({ queryKey: ["projects"] }); navigate("/projects"); },
    onError: (e) => toast("error", (e as Error).message),
  });

  const submitGeneral = (e: FormEvent) => { e.preventDefault(); save.mutate({ name, description: description || null }); };
  const submitLimits = (e: FormEvent) => {
    e.preventDefault();
    save.mutate(custom
      ? { rate_limit_per_second: rps ? Number(rps) : null, rate_limit_per_minute: rpm ? Number(rpm) : null }
      : { use_default_rate_limits: true });
  };

  const active = project.status === "active";
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Panel title="Hizmet durumu" className="lg:col-span-2">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <div className="font-medium">{active ? "Proje istek kabul ediyor" : "Proje durduruldu"}</div>
            <p className="text-sm text-muted">{active ? "Durdurduğunuzda bu projenin tüm anahtarlarıyla gelen istekler 403 ile reddedilir." : "Bu projenin tüm istekleri şu anda 403 ile reddediliyor."}</p>
          </div>
          <Switch label="Proje durumu" checked={active} disabled={!isAdmin} onChange={() => setConfirmToggle(true)} />
        </div>
      </Panel>

      <Panel title="Genel bilgiler">
        <form onSubmit={submitGeneral} className="space-y-4">
          <Field label="Proje adı">{(id) => <Input id={id} required minLength={2} value={name} disabled={!isAdmin} onChange={(e) => setName(e.target.value)} />}</Field>
          <Field label="Slug" hint="Slug değiştirilemez.">{(id) => <Input id={id} value={project.slug} disabled className="font-mono" />}</Field>
          <Field label="Açıklama">{(id) => <Textarea id={id} rows={2} value={description} disabled={!isAdmin} onChange={(e) => setDescription(e.target.value)} />}</Field>
          {isAdmin && <Button type="submit" variant="primary" loading={save.isPending}>Kaydet</Button>}
        </form>
      </Panel>

      <Panel title="Hız sınırı" description="Bu projenin tüm anahtarları için toplam istek sınırı.">
        <form onSubmit={submitLimits} className="space-y-4">
          <label className="flex items-center gap-3 text-sm">
            <Switch label="Özel sınır" checked={custom} disabled={!isAdmin} onChange={setCustom} />
            Bu projeye özel sınır kullan
          </label>
          {custom ? (
            <div className="grid grid-cols-2 gap-3">
              <Field label="Saniyede">{(id) => <Input id={id} type="number" min={1} value={rps} disabled={!isAdmin} onChange={(e) => setRps(e.target.value)} placeholder={String(platform.data?.default_rate_limit_per_second ?? "")} />}</Field>
              <Field label="Dakikada">{(id) => <Input id={id} type="number" min={1} value={rpm} disabled={!isAdmin} onChange={(e) => setRpm(e.target.value)} placeholder={String(platform.data?.default_rate_limit_per_minute ?? "")} />}</Field>
            </div>
          ) : (
            <p className="text-sm text-muted">
              Platform varsayılanı kullanılıyor: saniyede <strong className="text-ink">{num(platform.data?.default_rate_limit_per_second)}</strong>, dakikada <strong className="text-ink">{num(platform.data?.default_rate_limit_per_minute)}</strong> istek.
            </p>
          )}
          {isAdmin && <Button type="submit" variant="primary" loading={save.isPending}>Kaydet</Button>}
        </form>
      </Panel>

      {isAdmin && (
        <section className="rounded-lg border border-block/30 bg-surface lg:col-span-2">
          <div className="flex flex-wrap items-center justify-between gap-4 p-5">
            <div>
              <h2 className="text-sm font-semibold text-block-ink">Projeyi sil</h2>
              <p className="text-sm text-muted">Projenin tüm anahtarları, kararları, inceleme kayıtları ve IP kuralları kalıcı olarak silinir. Denetim kaydı saklanır.</p>
            </div>
            <Button variant="danger" onClick={() => setDeleting(true)}>Projeyi sil</Button>
          </div>
        </section>
      )}

      <Modal open={confirmToggle} onClose={() => setConfirmToggle(false)} title={active ? "Projeyi durdur" : "Projeyi başlat"}
        description={active ? `${project.name} uygulamasından gelen tüm moderasyon istekleri reddedilecek. Uygulamanız içerik yayınlarken buna hazırlıklı olmalı.` : `${project.name} tekrar istek kabul etmeye başlayacak.`}
        footer={<><Button onClick={() => setConfirmToggle(false)}>Vazgeç</Button><Button variant={active ? "danger" : "primary"} loading={toggle.isPending} onClick={() => toggle.mutate()}>{active ? "Durdur" : "Başlat"}</Button></>} />

      <Modal open={deleting} onClose={() => { setDeleting(false); setConfirmSlug(""); }} title={`${project.name} projesini sil`}
        description="Bu işlem geri alınamaz."
        footer={<><Button onClick={() => { setDeleting(false); setConfirmSlug(""); }}>Vazgeç</Button><Button variant="danger" disabled={confirmSlug !== project.slug} loading={remove.isPending} onClick={() => remove.mutate()}>Kalıcı olarak sil</Button></>}>
        <Field label={`Onaylamak için ${project.slug} yazın`}>
          {(id) => <Input id={id} className="font-mono" value={confirmSlug} onChange={(e) => setConfirmSlug(e.target.value)} autoComplete="off" />}
        </Field>
      </Modal>
    </div>
  );
}

// ---------------------------------------------------------------- Page

type TabKey = "overview" | "policy" | "keys" | "ip" | "webhook" | "integration" | "settings";

export function ProjectDetailPage() {
  const { id = "" } = useParams();
  const { user } = useAuth();
  const [tab, setTab] = useState<TabKey>("overview");
  const [range, setRange] = useState<RangeKey>("7d");
  const q = useQuery({ queryKey: ["project", id], queryFn: () => api<Project>(`/projects/${id}`) });
  const isAdmin = can(user?.role, "admin");

  if (q.isLoading) return <Spinner />;
  if (q.error) return <ErrorBox error={q.error} onRetry={() => q.refetch()} />;
  const p = q.data!;

  const tabs: { value: TabKey; label: string }[] = [
    { value: "overview", label: "Genel bakış" },
    { value: "policy", label: "Policy" },
    ...(isAdmin ? [
      { value: "keys" as const, label: "API anahtarları" },
      { value: "ip" as const, label: "IP engelleri" },
      { value: "webhook" as const, label: "Webhook" },
    ] : []),
    { value: "integration", label: "Entegrasyon" },
    { value: "settings", label: "Ayarlar" },
  ];

  return (
    <>
      <Link to="/projects" className="mb-3 inline-flex items-center gap-1.5 text-sm text-muted hover:text-ink"><Icon icon={ArrowLeft01Icon} size={16} />Projeler</Link>
      <header className="mb-5 flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-xl font-semibold">{p.name}</h1>
            {p.status === "active" ? <Tag tone="good">Aktif</Tag> : <Tag tone="bad">Durduruldu</Tag>}
          </div>
          <p className="mt-1 text-muted">{p.description || <span className="font-mono text-sm">{p.slug}</span>}</p>
        </div>
        <div className="flex items-center gap-2 text-sm text-muted">
          <Icon icon={Key01Icon} size={16} /> {p.active_keys} aktif anahtar · oluşturma {dateTime(p.created_at)}
        </div>
      </header>

      <Tabs value={tab} onChange={setTab} tabs={tabs} />

      {tab === "overview" && (
        <>
          <div className="mb-4 flex justify-end"><Segmented label="Zaman aralığı" value={range} onChange={setRange} options={RANGE_OPTIONS} /></div>
          <OverviewView range={range} projectId={p.id} />
        </>
      )}
      {tab === "keys" && <KeysTab projectId={p.id} />}
      {tab === "ip" && <IpRulesManager projectId={p.id} />}
      {tab === "webhook" && <WebhookTab projectId={p.id} />}
      {tab === "policy" && <PolicyTab projectId={p.id} />}
      {tab === "integration" && <IntegrationTab />}
      {tab === "settings" && <SettingsTab project={p} />}
    </>
  );
}

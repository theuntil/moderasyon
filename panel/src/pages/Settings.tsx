import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";

import { api, patch } from "../api";
import { useAuth } from "../auth";
import { useToast } from "../components/toast";
import { Button, ErrorBox, Field, Input, Modal, PageHeader, Panel, Spinner, Switch, Table, Textarea } from "../components/ui";
import { can, dateTime, num } from "../format";
import type { PlatformSettings } from "../types";
import { AutoBanPanel } from "./AutoBan";

/** Eşiklerin 0–1 skor ölçeğinde hangi aralığı hangi karara ayırdığını gösterir */
function ThresholdScale({ review, block }: { review: number; block: number }) {
  const r = Math.min(Math.max(review, 0), 1) * 100;
  const b = Math.min(Math.max(block, 0), 1) * 100;
  const valid = r < b;
  return (
    <div aria-hidden>
      <div className="relative flex h-3 overflow-hidden rounded-full">
        <div className="bg-allow" style={{ width: `${valid ? r : 100}%` }} />
        {valid && <div className="bg-review" style={{ width: `${b - r}%` }} />}
        {valid && <div className="flex-1 bg-block" />}
      </div>
      <div className="relative mt-1 h-4 text-[11px] text-muted">
        <span className="absolute left-0">0</span>
        {valid && <span className="absolute -translate-x-1/2" style={{ left: `${r}%` }}>{review}</span>}
        {valid && <span className="absolute -translate-x-1/2" style={{ left: `${b}%` }}>{block}</span>}
        <span className="absolute right-0">1</span>
      </div>
      <div className="mt-2 grid grid-cols-3 text-xs">
        <span className="text-allow-ink">İzin ver</span>
        <span className="text-center text-review-ink">İncelemeye gönder</span>
        <span className="text-right text-block-ink">Engelle</span>
      </div>
    </div>
  );
}

export function SettingsPage() {
  const { user } = useAuth();
  const toast = useToast();
  const qc = useQueryClient();
  const isAdmin = can(user?.role, "admin");
  const q = useQuery({ queryKey: ["settings"], queryFn: () => api<PlatformSettings>("/settings") });

  const [review, setReview] = useState("");
  const [block, setBlock] = useState("");
  const [rps, setRps] = useState("");
  const [rpm, setRpm] = useState("");
  const [message, setMessage] = useState("");
  const [retention, setRetention] = useState("");
  const [evidenceHours, setEvidenceHours] = useState("");
  const [confirmService, setConfirmService] = useState(false);

  useEffect(() => {
    if (!q.data) return;
    setReview(String(q.data.threshold_review));
    setBlock(String(q.data.threshold_block));
    setRps(String(q.data.default_rate_limit_per_second));
    setRpm(String(q.data.default_rate_limit_per_minute));
    setMessage(q.data.maintenance_message ?? "");
    setRetention(String(q.data.retention_days));
    setEvidenceHours(String(q.data.evidence_retention_hours));
  }, [q.data]);

  const save = useMutation({
    mutationFn: (body: object) => patch<{ policy_version: number }>("/settings", body),
    onSuccess: (r, body) => {
      const b = body as Record<string, unknown>;
      toast("success",
        "service_enabled" in b ? (b.service_enabled ? "Hizmet açıldı." : "Hizmet kapatıldı.")
          : "threshold_review" in b ? `Eşikler kaydedildi. Yeni policy: v${r.policy_version}` : "Ayarlar kaydedildi.");
      qc.invalidateQueries({ queryKey: ["settings"] });
      qc.invalidateQueries({ queryKey: ["system"] });
      setConfirmService(false);
    },
    onError: (e) => toast("error", (e as Error).message),
  });

  if (q.isLoading) return <Spinner />;
  if (q.error) return <ErrorBox error={q.error} onRetry={() => q.refetch()} />;
  const s = q.data!;
  const reviewN = Number(review);
  const blockN = Number(block);
  const thresholdsChanged = reviewN !== s.threshold_review || blockN !== s.threshold_block;

  const submitThresholds = (e: FormEvent) => { e.preventDefault(); save.mutate({ threshold_review: reviewN, threshold_block: blockN }); };
  const submitLimits = (e: FormEvent) => { e.preventDefault(); save.mutate({ default_rate_limit_per_second: Number(rps), default_rate_limit_per_minute: Number(rpm) }); };

  return (
    <>
      <PageHeader title="Platform ayarları" description={`Son değişiklik ${dateTime(s.updated_at)}${s.updated_by_email ? `, ${s.updated_by_email}` : ""}.`} />
      <div className="grid gap-6 lg:grid-cols-2">
        <Panel title="Moderasyon hizmeti" className="lg:col-span-2">
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div>
              <div className="font-medium">{s.service_enabled ? "Hizmet açık" : "Hizmet kapalı"}</div>
              <p className="max-w-[70ch] text-sm text-muted">
                Kapattığınızda tüm projelerin istekleri 503 ile reddedilir. Bakım veya acil durumlar için kullanın. Değişiklik birkaç saniye içinde tüm sunuculara yansır.
              </p>
            </div>
            <Switch label="Moderasyon hizmeti" checked={s.service_enabled} disabled={!isAdmin} onChange={() => setConfirmService(true)} />
          </div>
        </Panel>

        <Panel title="Karar eşikleri" description={`Şu an geçerli: policy v${s.policy_version}. Her değişiklik yeni bir sürüm oluşturur.`}>
          <form onSubmit={submitThresholds} className="space-y-5">
            <ThresholdScale review={reviewN} block={blockN} />
            <div className="grid grid-cols-2 gap-3">
              <Field label="İnceleme eşiği" hint="Bu skorun üstü incelemeye gider.">
                {(id) => <Input id={id} type="number" step="0.01" min="0.01" max="0.99" value={review} disabled={!isAdmin} onChange={(e) => setReview(e.target.value)} />}
              </Field>
              <Field label="Engelleme eşiği" hint="Bu skorun üstü doğrudan engellenir.">
                {(id) => <Input id={id} type="number" step="0.01" min="0.01" max="1" value={block} disabled={!isAdmin} onChange={(e) => setBlock(e.target.value)} />}
              </Field>
            </div>
            <p className="text-xs text-muted">Eşikleri gerçek verilerle test etmeden değiştirmeyin. Düşük engelleme eşiği masum içerikleri de engelleyebilir.</p>
            {isAdmin && <Button type="submit" variant="primary" disabled={!thresholdsChanged || reviewN >= blockN} loading={save.isPending}>Yeni policy olarak kaydet</Button>}
          </form>
        </Panel>

        <Panel title="Varsayılan hız sınırı" description="Kendi sınırı tanımlanmamış projeler için geçerlidir.">
          <form onSubmit={submitLimits} className="space-y-4">
            <div className="grid grid-cols-2 gap-3">
              <Field label="Saniyede istek">{(id) => <Input id={id} type="number" min={1} value={rps} disabled={!isAdmin} onChange={(e) => setRps(e.target.value)} />}</Field>
              <Field label="Dakikada istek">{(id) => <Input id={id} type="number" min={1} value={rpm} disabled={!isAdmin} onChange={(e) => setRpm(e.target.value)} />}</Field>
            </div>
            {isAdmin && <Button type="submit" variant="primary" loading={save.isPending}>Kaydet</Button>}
          </form>
        </Panel>

        <Panel title="Koruma ayarları" className="lg:col-span-2">
          <div className="divide-y divide-line">
            {([
              ["legal_hold_enabled", "Kritik içeriği yasal saklamaya al",
                "Pornografi, çocuk istismarı, terör ve 'kritik' işaretli kurallara takılan içeriğin orijinal medyası, metni, IP'si ve kullanıcı bilgileri siz silene kadar saklanır (olası dava/şikâyet için). Kapalıysa normal saklama süresi uygulanır."],
              ["visual_rules_enabled", "Yasaklı görsel/sembol kontrolü",
                "Kurallar sayfasında tanımlı sembol, bayrak ve amblemler her görsel ve videoda AI ile aranır (API anahtarı gerekir, bütçeden düşer)."],
              ["ai_sensitive_media", "Belirsiz çıplaklıkta AI'a danış",
                "Yerel model çıplaklıktan emin olamazsa (risk %50–85) kareler AI'a sorulur. Kesin tespit edilen çıplaklık zaten engellenir ve dışarı gönderilmez."],
            ] as const).map(([key, title, help]) => (
              <div key={key} className="flex items-start justify-between gap-6 py-3 first:pt-0 last:pb-0">
                <div><div className="text-sm font-medium">{title}</div><p className="mt-0.5 max-w-[70ch] text-xs text-muted">{help}</p></div>
                <Switch label={title} checked={s[key]} disabled={!isAdmin} onChange={(v) => save.mutate({ [key]: v })} />
              </div>
            ))}
          </div>
        </Panel>

        <AutoBanPanel s={s} />

        <Panel title="Medya saklama" className="lg:col-span-2"
          description="Tüm medya Cloudflare R2'de tutulur; sunucunun diskine medya yazılmaz. Orijinal görsel/video analiz biter bitmez R2'den silinir. İzin verilen içerikten hiçbir şey tutulmaz.">
          <form onSubmit={(e) => { e.preventDefault(); save.mutate({ evidence_retention_hours: Number(evidenceHours) }); }} className="space-y-3">
            <ul className="list-disc space-y-1 pl-5 text-sm text-muted">
              <li>Engellenen veya incelemeye düşen içeriğin <strong className="text-ink">küçültülmüş, konum bilgisi silinmiş</strong> kanıt kareleri tutulur (içerik başına en fazla 6 kare, ~50–100 KB).</li>
              <li>İnceleme bekleyen içeriğin kanıtı, moderatör karar verene kadar silinmez.</li>
              <li>Karar verildikten sonra aşağıdaki süre dolunca kanıt otomatik silinir (15 dakikada bir kontrol edilir).</li>
              <li>Güvenlik ağı: R2 bucket'ında otomatik silme kuralları vardır (yüklemeler 1 gün, kanıtlar en geç 35 gün).</li>
            </ul>
            <div className="flex flex-wrap items-end gap-3">
              <Field label="Kanıt saklama süresi (saat)" hint="0: otomatik engellenen içerik için hiç kanıt saklanmaz. Varsayılan 72 saat (itiraz/denetim için).">
                {(id) => <Input id={id} type="number" min={0} max={8760} className="w-32" value={evidenceHours} disabled={!isAdmin} onChange={(e) => setEvidenceHours(e.target.value)} />}
              </Field>
              {isAdmin && <Button type="submit" variant="primary" loading={save.isPending} disabled={Number(evidenceHours) === s.evidence_retention_hours}>Kaydet</Button>}
            </div>
          </form>
        </Panel>

        <Panel title="Veri saklama süresi" className="lg:col-span-2"
          description="Süresi dolan içeriklerin metni, URL'si, metadata'sı, istemci IP'si ve kanıt görselleri silinir. Karar ve istatistik kayıtları kalır. İncelemesi bekleyen içeriğe dokunulmaz.">
          <form onSubmit={(e) => { e.preventDefault(); save.mutate({ retention_days: Number(retention) }); }} className="flex flex-wrap items-end gap-3">
            <Field label="Gün" hint="KVKK: amaç için gereken en kısa süreyi seçin. Varsayılan 30 gün.">
              {(id) => <Input id={id} type="number" min={1} max={3650} className="w-32" value={retention} disabled={!isAdmin} onChange={(e) => setRetention(e.target.value)} />}
            </Field>
            {isAdmin && <Button type="submit" variant="primary" loading={save.isPending} disabled={Number(retention) === s.retention_days}>Kaydet</Button>}
          </form>
        </Panel>

        <Panel title="Policy geçmişi" className="lg:col-span-2" padded={false} description="Her karar hangi policy sürümüyle verildiğini saklar.">
          <Table>
            <thead><tr><th>Sürüm</th><th>İnceleme eşiği</th><th>Engelleme eşiği</th><th>Oluşturan</th><th>Tarih</th></tr></thead>
            <tbody>
              {s.policy_versions.map((v) => (
                <tr key={v.version}>
                  <td className="font-medium">v{v.version}{v.version === s.policy_version && <span className="ml-2 text-xs text-allow-ink">geçerli</span>}</td>
                  <td>{num(v.threshold_review)}</td>
                  <td>{num(v.threshold_block)}</td>
                  <td className="text-muted">{v.created_by_email ?? "kurulum"}</td>
                  <td className="text-muted">{dateTime(v.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </Table>
        </Panel>
      </div>

      <Modal open={confirmService} onClose={() => setConfirmService(false)}
        title={s.service_enabled ? "Hizmeti kapat" : "Hizmeti aç"}
        description={s.service_enabled ? "Tüm projelerden gelen moderasyon istekleri reddedilecek. Uygulamalar içerik yayınlayamayabilir." : "Moderasyon API'si tekrar istek kabul etmeye başlayacak."}
        footer={<><Button onClick={() => setConfirmService(false)}>Vazgeç</Button>
          <Button variant={s.service_enabled ? "danger" : "primary"} loading={save.isPending}
            onClick={() => save.mutate(s.service_enabled ? { service_enabled: false, maintenance_message: message || null } : { service_enabled: true })}>
            {s.service_enabled ? "Hizmeti kapat" : "Hizmeti aç"}
          </Button></>}>
        {s.service_enabled && (
          <Field label="Uygulamalara gösterilecek mesaj" hint="API yanıtındaki message alanında döner.">
            {(id) => <Textarea id={id} rows={2} maxLength={300} value={message} onChange={(e) => setMessage(e.target.value)} placeholder="Planlı bakım çalışması" />}
          </Field>
        )}
      </Modal>
    </>
  );
}

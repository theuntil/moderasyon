import { useQuery } from "@tanstack/react-query";
import { RefreshIcon } from "@hugeicons/core-free-icons";
import type { ReactNode } from "react";

import { api } from "../api";
import { Icon } from "../components/icon";
import { Button, ErrorBox, PageHeader, Panel, Spinner, Stat } from "../components/ui";
import { dateTime, num } from "../format";
import type { SystemHealth } from "../types";

function Check({ name, ok, detail, children }: { name: string; ok: boolean; detail: ReactNode; children?: ReactNode }) {
  return (
    <div className="rounded-lg border border-line bg-surface p-5">
      <div className="flex items-center justify-between">
        <h2 className="font-semibold">{name}</h2>
        <span className={`inline-flex items-center gap-1.5 text-sm font-medium ${ok ? "text-allow-ink" : "text-block-ink"}`}>
          <span className={`size-2 rounded-full ${ok ? "bg-allow" : "bg-block"}`} />
          {ok ? "Çalışıyor" : "Sorun var"}
        </span>
      </div>
      <p className="mt-1 text-sm text-muted">{detail}</p>
      {children && <div className="mt-4">{children}</div>}
    </div>
  );
}

export function SystemPage() {
  const q = useQuery({ queryKey: ["system"], queryFn: () => api<SystemHealth>("/system"), refetchInterval: 15_000 });

  return (
    <>
      <PageHeader
        title="Sistem durumu"
        description={q.data ? `Son kontrol ${dateTime(q.data.checked_at)}. Her 15 saniyede yenilenir.` : "Altyapı bileşenlerinin anlık durumu."}
        actions={<Button icon={<Icon icon={RefreshIcon} size={16} />} onClick={() => q.refetch()} loading={q.isFetching}>Yenile</Button>}
      />
      {q.isLoading ? <Spinner /> : q.error ? <ErrorBox error={q.error} onRetry={() => q.refetch()} /> : (() => {
        const s = q.data!;
        const queue = s.queue ?? {};
        const stuck = queue.stuck ?? 0;
        return (
          <div className="grid gap-4 md:grid-cols-2">
            <Check name="Moderasyon hizmeti" ok={!!s.service?.service_enabled}
              detail={s.service?.service_enabled ? `İstekler kabul ediliyor. Policy v${s.service.policy_version}, saklama ${s.service.retention_days} gün.` : "Hizmet panelden kapatıldı; API istekleri 503 ile reddediyor."} />

            <Check name="Metin işçisi" ok={!!s.worker?.ok}
              detail={s.worker?.ok ? "İçerikleri analiz eden arka plan işçileri çalışıyor." : "Son 60 saniyede worker'dan sinyal alınamadı. İstekler kuyrukta birikiyor olabilir."}>
              {s.worker?.heartbeat && <p className="break-all font-mono text-xs text-muted">{s.worker.heartbeat}</p>}
            </Check>

            <Check name="Medya işçisi" ok={!!s.media_worker?.ok}
              detail={s.media_worker?.ok ? "Görsel ve video analizi çalışıyor." : "Medya işçisinden sinyal alınamadı. Görsel/video istekleri bekliyor olabilir."}>
              <Stat label="Medya kuyruğu" value={num(queue.media_depth)} tone={(queue.media_depth ?? 0) > 50 ? "review" : undefined} />
            </Check>

            <Check name="Cloudflare R2 (medya deposu)" ok={!!s.storage?.ok}
              detail={!s.storage?.configured ? "R2 yapılandırılmamış (R2_* ortam değişkenleri). Metin çalışır, görsel/video kabul edilmez."
                : s.storage.ok ? `Bucket: ${s.storage.bucket}. Medya sunucuda değil, R2'de tutulur.` : `R2'ye erişilemiyor (${s.storage.error}). Görsel/video istekleri 503 + retryable alır.`}>
              {s.storage && (
                <div className="grid grid-cols-3 gap-2">
                  <Stat label="Yanıt süresi" value={s.storage.latency_ms != null ? `${s.storage.latency_ms} ms` : "—"} />
                  <Stat label="Kanıt karesi" value={num(s.storage.evidence_files)} hint={`${num(s.storage.evidence_pending_review)} inceleme bekliyor`} />
                  <Stat label="İşlenmeyi bekleyen" value={num(s.storage.incoming_files)} />
                </div>
              )}
            </Check>

            <Check name="Webhook" ok={(s.webhooks?.failed_24h ?? 0) === 0}
              detail={(s.webhooks?.failed_24h ?? 0) === 0 ? "Son 24 saatte başarısız teslimat yok." : "Bazı uygulamalar bildirimleri kabul etmiyor. Proje > Webhook sekmesine bakın."}>
              <div className="grid grid-cols-2 gap-2">
                <Stat label="Bekleyen" value={num(s.webhooks?.pending)} />
                <Stat label="Başarısız (24s)" value={num(s.webhooks?.failed_24h)} tone={s.webhooks?.failed_24h ? "block" : undefined} />
              </div>
            </Check>

            <Check name="Kuyruk" ok={stuck === 0}
              detail={stuck === 0 ? "Bekleyen iş normal sürede işleniyor." : `${num(stuck)} istek 5 dakikadan uzun süredir kuyrukta bekliyor.`}>
              <div className="grid grid-cols-4 gap-2">
                <Stat label="Kuyrukta" value={num(queue.depth)} />
                <Stat label="İşlenen" value={num(queue.in_progress)} />
                <Stat label="Takılan" value={num(stuck)} tone={stuck ? "block" : undefined} />
                <Stat label="Hata (24s)" value={num(queue.failed_24h)} tone={queue.failed_24h ? "block" : undefined} />
              </div>
            </Check>

            <Check name="PostgreSQL" ok={s.database.ok} detail={s.database.ok ? `Sürüm ${s.database.version}` : `Bağlantı hatası: ${s.database.error}`}>
              {s.database.ok && (
                <div className="grid grid-cols-2 gap-2">
                  <Stat label="Yanıt süresi" value={`${s.database.latency_ms} ms`} />
                  <Stat label="Veritabanı boyutu" value={s.database.size} />
                </div>
              )}
            </Check>

            <Check name="Redis" ok={s.redis.ok} detail={s.redis.ok ? "Kuyruk ve hız sınırı deposu." : `Bağlantı hatası: ${s.redis.error}`}>
              {s.redis.ok && (
                <div className="grid grid-cols-2 gap-2">
                  <Stat label="Yanıt süresi" value={`${s.redis.latency_ms} ms`} />
                  <Stat label="Bellek" value={s.redis.memory} />
                </div>
              )}
            </Check>

            <Panel title="Sorun giderme">
              <ul className="space-y-2 text-sm text-muted">
                <li><strong className="font-medium text-ink">Worker çalışmıyorsa:</strong> Dokploy'da worker servisinin loglarına bakın ve servisi yeniden başlatın.</li>
                <li><strong className="font-medium text-ink">Takılan istek varsa:</strong> worker her dakika bunları otomatik olarak kuyruğa geri ekler.</li>
                <li><strong className="font-medium text-ink">Hatalar artıyorsa:</strong> Kararlar sayfasında durumu "Hata" olanları filtreleyip hata mesajlarına bakın.</li>
              </ul>
            </Panel>
          </div>
        );
      })()}
    </>
  );
}

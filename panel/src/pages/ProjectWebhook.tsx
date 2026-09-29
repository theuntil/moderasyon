import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";

import { api, ApiError, patch, post } from "../api";
import { useToast } from "../components/toast";
import {
  Button, Empty, ErrorBox, Field, Input, Modal, Mono, Panel, SecretReveal, Spinner, Stat, Switch, Table, Tag,
} from "../components/ui";
import { dateTime, num, relative } from "../format";
import type { WebhookInfo } from "../types";

const EVENT_LABEL: Record<string, string> = {
  "moderation.completed": "Karar hazır",
  "moderation.reviewed": "İnsan kararı",
  "moderation.failed": "İşlenemedi",
};

export function WebhookTab({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery({ queryKey: ["webhook", projectId], queryFn: () => api<WebhookInfo>(`/projects/${projectId}/webhook`), refetchInterval: 15_000 });
  const [url, setUrl] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [secret, setSecret] = useState<string | null>(null);
  const [confirmRotate, setConfirmRotate] = useState(false);
  useEffect(() => { if (q.data) setUrl(q.data.webhook_url ?? ""); }, [q.data?.webhook_url]); // eslint-disable-line react-hooks/exhaustive-deps

  const refresh = () => qc.invalidateQueries({ queryKey: ["webhook", projectId] });
  const save = useMutation({
    mutationFn: (body: { url?: string; enabled?: boolean }) => patch<{ secret: string | null }>(`/projects/${projectId}/webhook`, body),
    onSuccess: (r) => { setError(null); toast("success", "Webhook ayarları kaydedildi."); if (r.secret) setSecret(r.secret); refresh(); },
    onError: (e) => setError((e as ApiError).message),
  });
  const rotate = useMutation({
    mutationFn: () => post<{ secret: string }>(`/projects/${projectId}/webhook/rotate-secret`),
    onSuccess: (r) => { setSecret(r.secret); setConfirmRotate(false); refresh(); },
    onError: (e) => toast("error", (e as Error).message),
  });
  const test = useMutation({
    mutationFn: () => post(`/projects/${projectId}/webhook/test`),
    onSuccess: () => { toast("success", "Test olayı kuyruğa alındı. Birkaç saniye içinde aşağıda görünür."); setTimeout(refresh, 2500); },
    onError: (e) => toast("error", (e as Error).message),
  });

  if (q.isLoading) return <Spinner />;
  if (q.error) return <ErrorBox error={q.error} onRetry={() => q.refetch()} />;
  const w = q.data!;
  const submit = (e: FormEvent) => { e.preventDefault(); save.mutate({ url: url.trim() }); };

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
      <div className="space-y-6">
        <Panel title="Webhook" description="Karar hazır olduğunda ve moderatör karar verdiğinde uygulamanıza anında bildirim gönderilir. Görsel/videoda önerilen yöntem budur.">
          <form onSubmit={submit} className="space-y-4">
            <div className="flex items-center justify-between gap-4">
              <div>
                <div className="text-sm font-medium">{w.webhook_enabled ? "Gönderim açık" : "Gönderim kapalı"}</div>
                <p className="text-xs text-muted">Kapalıyken olaylar kaydedilmez.</p>
              </div>
              <Switch label="Webhook gönderimi" checked={w.webhook_enabled} onChange={(v) => save.mutate({ enabled: v })} />
            </div>
            <Field label="Adres" hint="Herkese açık bir https adresi. İç ağ adreslerine gönderim yapılmaz." error={error ?? undefined}>
              {(id) => <Input id={id} type="url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://uygulamaniz.com/webhooks/moderation" className="font-mono" />}
            </Field>
            <div className="flex flex-wrap gap-2">
              <Button type="submit" variant="primary" loading={save.isPending}>Kaydet</Button>
              <Button type="button" onClick={() => test.mutate()} loading={test.isPending} disabled={!w.webhook_enabled}>Test olayı gönder</Button>
              {w.has_secret && <Button type="button" variant="ghost" onClick={() => setConfirmRotate(true)}>İmza anahtarını yenile</Button>}
            </div>
          </form>
        </Panel>

        <Panel title="Son teslimatlar" padded={false}>
          {w.deliveries.length === 0 ? <Empty title="Henüz gönderim yok" /> : (
            <Table>
              <thead><tr><th>Olay</th><th>İçerik</th><th>Durum</th><th>Deneme</th><th>Zaman</th></tr></thead>
              <tbody>
                {w.deliveries.map((d) => (
                  <tr key={d.id}>
                    <td>{EVENT_LABEL[d.event] ?? d.event}</td>
                    <td><Mono className="text-muted">{d.request_public_id ?? "—"}</Mono></td>
                    <td>
                      {d.status === "delivered" ? <Tag tone="good">Teslim edildi</Tag> : d.status === "failed" ? <Tag tone="bad">Başarısız</Tag> : <Tag tone="warn">Bekliyor</Tag>}
                      {(d.last_error || d.last_status_code) && d.status !== "delivered" && (
                        <div className="mt-0.5 text-xs text-muted">{d.last_error ?? `HTTP ${d.last_status_code}`}{d.status === "pending" && ` · sonraki ${relative(d.next_attempt_at)}`}</div>
                      )}
                    </td>
                    <td>{d.attempts}</td>
                    <td className="text-muted" title={dateTime(d.created_at)}>{relative(d.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
        </Panel>
      </div>

      <div className="space-y-6">
        <Panel title="Son 7 gün">
          <div className="grid grid-cols-3 gap-2">
            <Stat label="Teslim" value={num(w.stats_7d.delivered)} />
            <Stat label="Bekleyen" value={num(w.stats_7d.pending)} tone={w.stats_7d.pending ? "review" : undefined} />
            <Stat label="Başarısız" value={num(w.stats_7d.failed)} tone={w.stats_7d.failed ? "block" : undefined} />
          </div>
        </Panel>
        <Panel title="İmza doğrulama">
          <p className="text-sm text-muted">
            Her istekte <Mono>X-Moderation-Signature: t=…,v1=…</Mono> başlığı gelir. <Mono>v1</Mono>, imza anahtarıyla
            hesaplanan <Mono>HMAC-SHA256(t + "." + gövde)</Mono> değeridir. 5 dakikadan eski zaman damgalarını reddedin.
            2xx dışında yanıt verirseniz 8 kez, artan aralıklarla (yaklaşık 1 gün) tekrar denenir.
          </p>
        </Panel>
      </div>

      <Modal open={!!secret} onClose={() => setSecret(null)} title="İmza anahtarı" footer={<Button variant="primary" onClick={() => setSecret(null)}>Kaydettim</Button>}>
        {secret && <SecretReveal value={secret} warning="Bu anahtar sadece şimdi gösteriliyor. Uygulamanızın ortam değişkenlerine kaydedin." />}
      </Modal>
      <Modal open={confirmRotate} onClose={() => setConfirmRotate(false)} title="İmza anahtarını yenile"
        description="Eski anahtarla imzalanmış istekleri doğrulayan uygulamanız, yeni anahtarı girene kadar gelen olayları reddeder."
        footer={<><Button onClick={() => setConfirmRotate(false)}>Vazgeç</Button><Button variant="primary" loading={rotate.isPending} onClick={() => rotate.mutate()}>Yenile</Button></>} />
    </div>
  );
}

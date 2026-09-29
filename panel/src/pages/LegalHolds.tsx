import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { api, ApiError, del } from "../api";
import { useAuth } from "../auth";
import { useToast } from "../components/toast";
import { Button, Empty, ErrorBox, Input, Modal, Mono, PageHeader, Panel, Spinner, Stat, Table } from "../components/ui";
import { bytes, can, CONTENT_TYPE_LABEL, dateTime, num } from "../format";

export type UserInfo = { id?: string; name?: string; surname?: string; username?: string; email?: string; phone?: string;
  extra?: Record<string, unknown> } | null;

type Hold = { public_id: string; content_type: string; preview: string | null; held_at: string; created_at: string;
  client_ip: string | null; external_user_id: string | null; external_content_id: string | null; has_original: boolean;
  media_mime: string | null; media_bytes: number | null; project_name: string; labels: string[]; severity: string;
  user_info: UserInfo };

export function UserInfoBlock({ info, userId }: { info: UserInfo; userId?: string | null }) {
  if (!info && !userId) return <span className="text-muted">—</span>;
  const name = [info?.name, info?.surname].filter(Boolean).join(" ");
  return (
    <div className="space-y-0.5 text-sm">
      {name && <div className="font-medium">{name}</div>}
      {info?.username && <div className="text-muted">@{info.username}</div>}
      {info?.email && <div>{info.email}</div>}
      {info?.phone && <div>{info.phone}</div>}
      {(info?.id || userId) && <Mono className="text-xs text-muted">ID: {info?.id ?? userId}</Mono>}
      {info?.extra && Object.keys(info.extra).length > 0 && (
        <div className="text-xs text-muted">{Object.entries(info.extra).map(([k, v]) => `${k}: ${String(v)}`).join(" · ")}</div>
      )}
    </div>
  );
}

export function LabelChips({ labels }: { labels: string[] | null | undefined }) {
  if (!labels?.length) return null;
  return (
    <span className="inline-flex flex-wrap gap-1">
      {labels.map((l) => (
        <span key={l} className={`rounded-sm px-1.5 py-0.5 text-[11px] font-medium ${l.startsWith("terör") || l === "çocuk_istismarı" || l === "müstehcen" ? "bg-block-soft text-block-ink" : "bg-sunken text-muted"}`}>{l}</span>
      ))}
    </span>
  );
}

export function LegalHoldsPage() {
  const { user } = useAuth();
  const isAdmin = can(user?.role, "admin");
  const qc = useQueryClient();
  const toast = useToast();
  const [label, setLabel] = useState("");
  const [deleting, setDeleting] = useState<Hold | null>(null);
  const [confirm, setConfirm] = useState("");
  const q = useQuery({ queryKey: ["legal-holds", label], queryFn: () => api<{ items: Hold[]; stats: { total: number; last_7d: number } }>(`/legal-holds${label ? `?label=${encodeURIComponent(label)}` : ""}`) });
  const remove = useMutation({
    mutationFn: (h: Hold) => del(`/legal-holds/${h.public_id}`, { confirm_id: confirm }),
    onSuccess: () => { toast("success", "Kalıcı olarak silindi."); setDeleting(null); setConfirm(""); qc.invalidateQueries({ queryKey: ["legal-holds"] }); },
    onError: (e) => toast("error", (e as ApiError).message),
  });

  return (
    <>
      <PageHeader title="Yasal saklama"
        description="Kritik içerikler (pornografi, çocuk istismarı, terör ve kritik işaretli kurallar): orijinal medya, metin, IP ve kullanıcı bilgileri siz silene kadar saklanır. Otomatik temizlik ve uygulamanın silme isteği bunlara dokunmaz."
        actions={<Input aria-label="Etikete göre filtrele" placeholder="Etiket (ör. terör:pkk)" className="w-56" value={label} onChange={(e) => setLabel(e.target.value.trim())} />} />
      {q.isLoading ? <Spinner /> : q.error ? <ErrorBox error={q.error} onRetry={() => q.refetch()} /> : (
        <div className="space-y-6">
          <div className="grid grid-cols-2 gap-4 sm:max-w-md">
            <Stat label="Toplam kayıt" value={num(q.data!.stats.total)} />
            <Stat label="Son 7 gün" value={num(q.data!.stats.last_7d)} tone={q.data!.stats.last_7d ? "block" : undefined} />
          </div>
          <p className="rounded-md bg-review-soft px-3 py-2 text-sm text-review-ink">
            Çocuk istismarı şüphesi taşıyan içerik panelde görüntülenemez ve indirilemez; derhal yetkililere bildirin (EGM İhbar: ihbarweb.org.tr / 155).
          </p>
          <Panel padded={false}>
            {q.data!.items.length === 0 ? <Empty title="Kayıt yok">Kritik içerik engellendiğinde burada görünür.</Empty> : (
              <Table>
                <thead><tr><th>Tarih</th><th>İçerik</th><th>Etiketler</th><th>Kullanıcı</th><th>IP</th><th /></tr></thead>
                <tbody>{q.data!.items.map((h) => (
                  <tr key={h.public_id}>
                    <td className="whitespace-nowrap text-sm">{dateTime(h.held_at)}<div className="text-xs text-muted">{h.project_name}</div></td>
                    <td className="max-w-[260px]">
                      <div className="text-sm">{CONTENT_TYPE_LABEL[h.content_type]}{h.media_bytes ? ` · ${bytes(h.media_bytes)}` : ""}</div>
                      {h.preview && <div className="truncate text-xs text-muted">{h.preview}</div>}
                      <Link to={`/decisions?q=${h.public_id}`} className="font-mono text-xs text-primary hover:underline">{h.public_id}</Link>
                    </td>
                    <td><LabelChips labels={h.labels} /></td>
                    <td><UserInfoBlock info={h.user_info} userId={h.external_user_id} /></td>
                    <td><Mono className="text-xs">{h.client_ip ?? "—"}</Mono></td>
                    <td className="space-y-1 text-right">
                      {isAdmin && h.has_original && !h.labels.includes("çocuk_istismarı") && (
                        <a href={`/api/legal-holds/${h.public_id}/original`} className="block text-sm text-primary hover:underline">Orijinali indir</a>
                      )}
                      {isAdmin && <Button size="sm" variant="ghostDanger" onClick={() => { setDeleting(h); setConfirm(""); }}>Kalıcı sil</Button>}
                    </td>
                  </tr>))}
                </tbody>
              </Table>
            )}
          </Panel>
        </div>
      )}
      <Modal open={!!deleting} onClose={() => setDeleting(null)} title="Kalıcı olarak sil"
        description="Orijinal medya, kanıt kareleri, metin, IP ve kullanıcı bilgileri silinir. Geri alınamaz; olası bir dava için delil kaybolur."
        footer={<><Button onClick={() => setDeleting(null)}>Vazgeç</Button>
          <Button variant="danger" disabled={confirm !== deleting?.public_id} loading={remove.isPending} onClick={() => deleting && remove.mutate(deleting)}>Kalıcı sil</Button></>}>
        <p className="mb-2 text-sm">Onaylamak için kimliği yazın: <Mono>{deleting?.public_id}</Mono></p>
        <Input value={confirm} onChange={(e) => setConfirm(e.target.value)} className="font-mono" />
      </Modal>
    </>
  );
}

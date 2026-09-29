import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, del } from "../api";
import { useAuth } from "../auth";
import { useToast } from "../components/toast";
import { Button, Empty, ErrorBox, Modal, Mono, PageHeader, Panel, Spinner, Tag } from "../components/ui";
import { can, dateTime, relative } from "../format";
import type { BlocklistItem } from "../types";

function Preview({ item }: { item: BlocklistItem }) {
  const [shown, setShown] = useState(false);
  if (!item.has_preview) return <div className="flex aspect-video items-center justify-center bg-sunken text-xs text-faint">Önizleme yok</div>;
  return (
    <button type="button" onClick={() => setShown(!shown)} className="relative block w-full bg-sunken" aria-label={shown ? "Gizle" : "Göster"}>
      <img src={`/api/media/blocklist/${item.id}`} alt="" loading="lazy"
        className={`aspect-video w-full object-contain ${shown ? "" : "scale-105 blur-2xl"}`} />
      {!shown && <span className="absolute inset-0 flex items-center justify-center text-xs font-medium text-white">Göster</span>}
    </button>
  );
}

export function BlocklistPage() {
  const { user } = useAuth();
  const qc = useQueryClient();
  const toast = useToast();
  const [removing, setRemoving] = useState<BlocklistItem | null>(null);
  const q = useQuery({ queryKey: ["blocklist"], queryFn: () => api<BlocklistItem[]>("/blocklist") });
  const remove = useMutation({
    mutationFn: (item: BlocklistItem) => del(`/blocklist/${item.id}`),
    onSuccess: () => { toast("success", "Engel kaldırıldı."); qc.invalidateQueries({ queryKey: ["blocklist"] }); setRemoving(null); },
    onError: (e) => toast("error", (e as Error).message),
  });

  return (
    <>
      <PageHeader
        title="Görsel engel listesi"
        description="İncelemede engellenip listeye eklenen görseller ve video kareleri. Aynı içerik yeniden boyutlandırılsa veya sıkıştırılsa bile tekrar yüklendiğinde otomatik engellenir."
      />
      {q.isLoading ? <Spinner /> : q.error ? <ErrorBox error={q.error} onRetry={() => q.refetch()} /> : q.data!.length === 0 ? (
        <Panel><Empty title="Liste boş">İnceleme kuyruğunda bir görseli engellerken "kopyaları da engellensin" seçeneğini işaretlediğinizde burada görünür.</Empty></Panel>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {q.data!.map((b) => (
            <article key={b.id} className="overflow-hidden rounded-lg border border-line bg-surface">
              <Preview item={b} />
              <div className="space-y-2 p-4 text-sm">
                <div className="flex flex-wrap items-center gap-1.5">
                  {b.project_id ? <Tag>{b.project_name}</Tag> : <Tag tone="info">Tüm platform</Tag>}
                  <Tag>{b.hash_count} parmak izi</Tag>
                </div>
                {b.reason && <p className="text-muted">{b.reason}</p>}
                <p className="text-xs text-muted" title={dateTime(b.created_at)}>
                  {b.created_by ?? "—"} · {relative(b.created_at)}
                </p>
                {b.source_public_id && <Mono className="block truncate text-faint">{b.source_public_id}</Mono>}
                {can(user?.role, "admin") && (
                  <Button size="sm" variant="ghostDanger" onClick={() => setRemoving(b)}>Listeden kaldır</Button>
                )}
              </div>
            </article>
          ))}
        </div>
      )}
      <Modal open={!!removing} onClose={() => setRemoving(null)} title="Engeli kaldır"
        description="Bu görsel ve kopyaları artık otomatik engellenmeyecek; normal analizden geçecek."
        footer={<><Button onClick={() => setRemoving(null)}>Vazgeç</Button><Button variant="danger" loading={remove.isPending} onClick={() => removing && remove.mutate(removing)}>Kaldır</Button></>} />
    </>
  );
}

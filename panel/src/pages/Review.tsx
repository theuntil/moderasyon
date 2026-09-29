import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ViewIcon, ViewOffIcon } from "@hugeicons/core-free-icons";
import { useState } from "react";

import { api, post } from "../api";
import { Icon } from "../components/icon";
import { useToast } from "../components/toast";
import { Button, DecisionBadge, Empty, ErrorBox, Mono, PageHeader, Panel, Select, Spinner, Tabs, Tag } from "../components/ui";
import { EvidenceGrid } from "../components/EvidenceGrid";
import { categoryLabel, CONTENT_TYPE_LABEL, dateTime, duration, relative } from "../format";
import type { Project, ReviewItem } from "../types";

function ReviewCard({ item }: { item: ReviewItem }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [hidden, setHidden] = useState(false);
  const isMedia = item.content_type !== "text";
  const [blocklist, setBlocklist] = useState(isMedia);
  const resolve = useMutation({
    mutationFn: (decision: "allow" | "block") =>
      post<{ blocklisted: boolean }>(`/review/${item.id}`, { decision, add_to_blocklist: decision === "block" && isMedia && blocklist }),
    onSuccess: (r, decision) => {
      toast("success", decision === "allow" ? "İçeriğe izin verildi."
        : r.blocklisted ? "Engellendi. Bu görselin kopyaları da otomatik engellenecek." : "İçerik engellendi.");
      qc.invalidateQueries({ queryKey: ["review"] });
      qc.invalidateQueries({ queryKey: ["review-summary"] });
      qc.invalidateQueries({ queryKey: ["overview"] });
    },
    onError: (e) => { toast("error", (e as Error).message); qc.invalidateQueries({ queryKey: ["review"] }); },
  });

  return (
    <article className="rounded-lg border border-line bg-surface">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line px-5 py-2.5 text-sm text-muted">
        <span className="font-medium text-ink">{item.project_name}</span>
        {isMedia && (
          <span>
            {CONTENT_TYPE_LABEL[item.content_type]}
            {item.media_duration_ms != null && ` · ${duration(item.media_duration_ms)}`}
            {item.media_width != null && ` · ${item.media_width}×${item.media_height}`}
          </span>
        )}
        <span>{relative(item.created_at)} geldi</span>
        {item.external_user_id && <span>kullanıcı <Mono>{item.external_user_id}</Mono></span>}
        {item.external_content_id && <span>içerik <Mono>{item.external_content_id}</Mono></span>}
        <span className="ml-auto"><Mono>{item.public_id}</Mono></span>
      </div>

      <div className="px-5 py-4">
        {isMedia ? (
          item.evidence.length ? <EvidenceGrid items={item.evidence} />
            : <p className="text-sm text-muted">Görsel saklanmamış veya saklama süresi dolmuş.</p>
        ) : (
          <div className="relative">
            <p className={`whitespace-pre-wrap break-words text-[15px] leading-relaxed ${hidden ? "select-none blur-sm" : ""}`}>
              {item.content_text || <span className="text-muted">(metin yok)</span>}
            </p>
            <button onClick={() => setHidden(!hidden)} className="mt-2 inline-flex items-center gap-1 text-xs text-muted hover:text-ink">
              {hidden ? <><Icon icon={ViewIcon} size={14} />İçeriği göster</> : <><Icon icon={ViewOffIcon} size={14} />İçeriği gizle</>}
            </button>
          </div>
        )}

        <div className="mt-4 flex flex-wrap items-center gap-2 text-sm">
          <span className="text-muted">Otomatik karar:</span>
          <DecisionBadge decision={item.ai_decision} />
          {item.categories?.map((c) => (
            <Tag key={c.name} tone="warn">{categoryLabel(c.name)} · {Math.round(c.score * 100)}%</Tag>
          ))}
          {item.policy_version && <span className="text-faint">policy {item.policy_version}</span>}
        </div>
      </div>

      {item.status === "pending" ? (
        <div className="flex flex-wrap items-center justify-end gap-2 border-t border-line bg-sunken/60 px-5 py-3">
          {isMedia && (
            <label className="mr-auto flex items-center gap-2 text-xs text-muted">
              <input type="checkbox" checked={blocklist} onChange={(e) => setBlocklist(e.target.checked)} className="size-4 accent-[hsl(var(--primary))]" />
              Engellersem kopyaları da otomatik engellensin
            </label>
          )}
          <Button variant="allow" loading={resolve.isPending && resolve.variables === "allow"} disabled={resolve.isPending} onClick={() => resolve.mutate("allow")}>İzin ver</Button>
          <Button variant="block" loading={resolve.isPending && resolve.variables === "block"} disabled={resolve.isPending} onClick={() => resolve.mutate("block")}>Engelle</Button>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2 border-t border-line bg-sunken/60 px-5 py-3 text-sm text-muted">
          <DecisionBadge decision={item.human_decision} human />
          <span>{item.reviewed_by_email} · {dateTime(item.reviewed_at)}</span>
        </div>
      )}
    </article>
  );
}

export function ReviewPage() {
  const [status, setStatus] = useState<"pending" | "resolved">("pending");
  const [projectId, setProjectId] = useState("");
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/projects") });
  const summary = useQuery({ queryKey: ["review-summary"], queryFn: () => api<{ pending: number }>("/review/summary") });
  const q = useQuery({
    queryKey: ["review", status, projectId],
    queryFn: () => api<ReviewItem[]>(`/review?status=${status}${projectId ? `&project_id=${projectId}` : ""}`),
    refetchInterval: status === "pending" ? 20_000 : false,
  });

  return (
    <>
      <PageHeader
        title="İnceleme kuyruğu"
        description="Otomatik sistemin emin olamadığı içerikler. Verdiğiniz karar uygulamaya iletilir ve gelecekte modeli değerlendirmek için saklanır."
        actions={
          <Select aria-label="Proje" value={projectId} onChange={(e) => setProjectId(e.target.value)} className="w-48">
            <option value="">Tüm projeler</option>
            {projects.data?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </Select>
        }
      />
      <Tabs value={status} onChange={setStatus} tabs={[{ value: "pending", label: "Bekleyen", count: summary.data?.pending }, { value: "resolved", label: "Karar verilen" }]} />
      {q.isLoading ? <Spinner /> : q.error ? <ErrorBox error={q.error} onRetry={() => q.refetch()} /> : q.data!.length === 0 ? (
        <Panel><Empty title={status === "pending" ? "Bekleyen içerik yok" : "Henüz karar verilmedi"}>{status === "pending" ? "Yeni içerikler geldikçe burada görünecek. Sayfa kendiliğinden yenilenir." : undefined}</Empty></Panel>
      ) : (
        <div className="space-y-4">
          {status === "pending" && <p className="text-sm text-muted">En eski içerik en üstte.</p>}
          {q.data!.map((item) => <ReviewCard key={item.id} item={item} />)}
        </div>
      )}
    </>
  );
}

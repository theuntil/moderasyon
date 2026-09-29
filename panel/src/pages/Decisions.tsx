import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Search01Icon } from "@hugeicons/core-free-icons";
import { useState, type FormEvent } from "react";

import { api } from "../api";
import { Icon } from "../components/icon";
import {
  Button, DecisionBadge, Empty, ErrorBox, Input, Modal, Mono, PageHeader, Panel, Select, Spinner, Table, Tag,
} from "../components/ui";
import { EvidenceGrid } from "../components/EvidenceGrid";
import { LabelChips, UserInfoBlock } from "./LegalHolds";
import { bytes, categoryLabel, CONTENT_TYPE_LABEL, dateTime, duration, ms, relative, STATUS_LABEL } from "../format";
import type { DecisionDetail, DecisionRow, Project } from "../types";

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (<><dt className="text-muted">{label}</dt><dd className="min-w-0 break-words">{children ?? "—"}</dd></>);
}

function DecisionModal({ publicId, onClose }: { publicId: string | null; onClose: () => void }) {
  const q = useQuery({ queryKey: ["decision", publicId], queryFn: () => api<DecisionDetail>(`/decisions/${publicId}`), enabled: !!publicId });
  const d = q.data;
  return (
    <Modal open={!!publicId} onClose={onClose} title="Karar ayrıntısı" description={publicId ? <Mono>{publicId}</Mono> : undefined} width="max-w-2xl">
      {q.isLoading ? <Spinner /> : q.error ? <ErrorBox error={q.error} /> : d && (
        <div className="space-y-5 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <DecisionBadge decision={d.human_decision ?? d.ai_decision} human={!!d.human_decision} />
            {d.human_decision && d.ai_decision && <span className="text-muted">otomatik karar: <DecisionBadge decision={d.ai_decision} /></span>}
            <Tag tone={d.status === "failed" ? "bad" : "neutral"}>{STATUS_LABEL[d.status] ?? d.status}</Tag>
            {d.severity === "critical" && <Tag tone="bad">Kritik</Tag>}
            {d.legal_hold && <Tag tone="warn">Yasal saklamada</Tag>}
            <LabelChips labels={d.labels} />
          </div>
          {d.evidence_restricted && (
            <p className="rounded-md bg-block-soft px-3 py-2 text-block-ink">Bu içerik yasal nedenle panelde görüntülenemez. Yetkililere bildirin (ihbarweb.org.tr / 155).</p>
          )}
          {(d.user_info || d.external_user_id) && (
            <div className="rounded-lg border border-line p-3"><div className="mb-1 text-xs text-muted">Gönderen kullanıcı</div><UserInfoBlock info={d.user_info} userId={d.external_user_id} /></div>
          )}
          {d.content_text && (
            <div className="whitespace-pre-wrap break-words rounded-lg border border-line bg-sunken p-3 text-[14px]">{d.content_text}</div>
          )}
          {d.evidence.length > 0 && <EvidenceGrid items={d.evidence} size={d.evidence.length > 2 ? "sm" : "md"} />}
          {d.content_type !== "text" && !d.evidence.length && (
            <p className="text-sm text-muted">
              {d.content_purged_at ? "Saklama süresi dolduğu için görsel silindi." : "İzin verilen medya saklanmaz."}
            </p>
          )}
          {d.content_purged_at && d.content_type === "text" && <p className="text-sm text-muted">Saklama süresi dolduğu için içerik silindi.</p>}
          {!!d.categories?.length && (
            <div className="flex flex-wrap gap-2">
              {d.categories.map((c) => <Tag key={c.name} tone="warn">{categoryLabel(c.name)} · {Math.round(c.score * 100)}%</Tag>)}
            </div>
          )}
          <dl className="grid grid-cols-[150px_1fr] gap-x-4 gap-y-2 text-sm">
            <Row label="Proje">{d.project_name}</Row>
            <Row label="Tür">{CONTENT_TYPE_LABEL[d.content_type] ?? d.content_type}{d.source === "url" ? " (URL)" : d.source === "upload" ? " (yükleme)" : ""}</Row>
            {d.content_type !== "text" && (
              <Row label="Medya">
                {[d.media_mime, d.media_width != null ? `${d.media_width}×${d.media_height}` : null,
                  d.media_duration_ms != null ? duration(d.media_duration_ms) : null,
                  d.media_frames ? `${d.media_frames} kare analiz edildi` : null, d.media_bytes ? bytes(d.media_bytes) : null]
                  .filter(Boolean).join(" · ") || "—"}
              </Row>
            )}
            <Row label="API anahtarı">{d.api_key_name} <Mono className="text-muted">({d.key_prefix}…)</Mono></Row>
            <Row label="İstemci IP">{d.client_ip && <Mono>{d.client_ip}</Mono>}</Row>
            <Row label="Kullanıcı ID">{d.external_user_id && <Mono>{d.external_user_id}</Mono>}</Row>
            <Row label="İçerik ID">{d.external_content_id && <Mono>{d.external_content_id}</Mono>}</Row>
            <Row label="Gerekçe">{d.reason && <Mono>{d.reason}</Mono>}</Row>
            <Row label="Policy">{d.policy_version}</Row>
            <Row label="AI (Layer 2)">{d.ai_used ? `Kullanıldı · ${ms(d.ai_latency_ms)} · yerel model kararı: ${d.layer1_decision ?? "—"}` : "Kullanılmadı"}</Row>
            <Row label="Modeller">{d.providers?.map((p) => `${p.provider}/${p.model} ${p.version}`).join(", ")}</Row>
            <Row label="Geliş">{dateTime(d.created_at)}</Row>
            <Row label="Toplam süre">{ms(d.latency_ms)} <span className="text-muted">(analiz {ms(d.processing_time_ms)})</span></Row>
            <Row label="Deneme">{d.attempts}{d.error && <span className="text-block-ink"> · {d.error}</span>}</Row>
            {d.idempotency_key && <Row label="Idempotency-Key"><Mono>{d.idempotency_key}</Mono></Row>}
            {d.reviewed_by_email && <Row label="İnceleyen">{d.reviewed_by_email} · {dateTime(d.reviewed_at)}</Row>}
          </dl>
          {d.metadata && Object.keys(d.metadata).length > 0 && (
            <pre className="overflow-x-auto rounded-lg bg-sunken p-3 font-mono text-xs">{JSON.stringify(d.metadata, null, 2)}</pre>
          )}
        </div>
      )}
    </Modal>
  );
}

export function DecisionsPage() {
  const [projectId, setProjectId] = useState("");
  const [decision, setDecision] = useState("");
  const [status, setStatus] = useState("");
  const [ctype, setCtype] = useState("");
  const [label, setLabel] = useState("");
  const [critical, setCritical] = useState(false);
  const initialQ = new URLSearchParams(window.location.search).get("q") ?? "";
  const [search, setSearch] = useState(initialQ);
  const [query, setQuery] = useState(initialQ);
  const [open, setOpen] = useState<string | null>(null);
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/projects") });

  const params = new URLSearchParams();
  if (projectId) params.set("project_id", projectId);
  if (decision) params.set("decision", decision);
  if (status) params.set("status", status);
  if (ctype) params.set("type", ctype);
  if (label) params.set("label", label);
  if (critical) params.set("critical", "true");
  if (query) params.set("q", query);

  const q = useInfiniteQuery({
    queryKey: ["decisions", params.toString()],
    queryFn: ({ pageParam }) => {
      const p = new URLSearchParams(params);
      if (pageParam) p.set("before", pageParam);
      return api<{ items: DecisionRow[]; next_before: string | null }>(`/decisions?${p}`);
    },
    initialPageParam: "",
    getNextPageParam: (last) => last.next_before ?? undefined,
  });
  const items = q.data?.pages.flatMap((p) => p.items) ?? [];

  const submit = (e: FormEvent) => { e.preventDefault(); setQuery(search.trim()); };

  return (
    <>
      <PageHeader title="Kararlar" description="Moderasyondan geçen tüm içerikler, en yenisi en üstte." />
      <Panel padded={false}>
        <div className="flex flex-wrap items-center gap-2 border-b border-line p-3">
          <form onSubmit={submit} className="relative min-w-[220px] flex-1">
            <Icon icon={Search01Icon} size={16} className="pointer-events-none absolute left-2.5 top-2.5 text-faint" />
            <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="İstek, içerik veya kullanıcı ID" className="pl-8" aria-label="Ara" />
          </form>
          <Select aria-label="Proje" value={projectId} onChange={(e) => setProjectId(e.target.value)} className="w-40">
            <option value="">Tüm projeler</option>
            {projects.data?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </Select>
          <Select aria-label="Karar" value={decision} onChange={(e) => setDecision(e.target.value)} className="w-36">
            <option value="">Tüm kararlar</option>
            <option value="allow">İzin</option>
            <option value="review">İncele</option>
            <option value="block">Engelle</option>
          </Select>
          <Input aria-label="Etiket" placeholder="Etiket" value={label} onChange={(e) => setLabel(e.target.value.trim())} className="w-36" />
          <label className="flex items-center gap-1.5 text-sm text-muted"><input type="checkbox" checked={critical} onChange={(e) => setCritical(e.target.checked)} className="size-4" />Sadece kritik</label>
          <Select aria-label="Tür" value={ctype} onChange={(e) => setCtype(e.target.value)} className="w-32">
            <option value="">Tüm türler</option>
            <option value="text">Metin</option>
            <option value="image">Görsel</option>
            <option value="video">Video</option>
          </Select>
          <Select aria-label="Durum" value={status} onChange={(e) => setStatus(e.target.value)} className="w-36">
            <option value="">Tüm durumlar</option>
            <option value="completed">Tamamlandı</option>
            <option value="queued">Kuyrukta</option>
            <option value="processing">İşleniyor</option>
            <option value="failed">Hata</option>
          </Select>
        </div>

        {q.isLoading ? <Spinner /> : q.error ? <div className="p-5"><ErrorBox error={q.error} onRetry={() => q.refetch()} /></div> : items.length === 0 ? (
          <Empty title="Sonuç yok">Filtreleri değiştirmeyi deneyin.</Empty>
        ) : (
          <>
            <Table>
              <thead><tr><th>İçerik</th><th>Karar</th><th>Proje</th><th>Kullanıcı</th><th>Süre</th><th>Zaman</th></tr></thead>
              <tbody>
                {items.map((r) => (
                  <tr key={r.public_id} onClick={() => setOpen(r.public_id)} className="cursor-pointer hover:bg-sunken/70">
                    <td className="max-w-[360px]">
                      <div className="truncate">
                        {r.content_type !== "text" && <span className="mr-1.5 rounded-sm bg-sunken px-1 text-[11px] text-muted">{CONTENT_TYPE_LABEL[r.content_type]}{r.media_duration_ms != null ? ` ${duration(r.media_duration_ms)}` : ""}</span>}
                        {r.preview || (r.content_type === "text" ? <span className="text-muted">(silindi)</span> : null)}
                      </div>
                      <Mono className="text-faint">{r.public_id}</Mono>
                    </td>
                    <td>
                      {r.status === "completed" ? <DecisionBadge decision={r.final_decision} human={!!r.human_decision} /> : <Tag tone={r.status === "failed" ? "bad" : "neutral"}>{STATUS_LABEL[r.status] ?? r.status}</Tag>}
                      {r.severity === "critical" && <span className="ml-1"><Tag tone="bad">Kritik</Tag></span>}
                      <div className="mt-1"><LabelChips labels={r.labels} /></div>
                    </td>
                    <td>{r.project_name}</td>
                    <td>{r.external_user_id ? <Mono>{r.external_user_id}</Mono> : <span className="text-faint">—</span>}</td>
                    <td className="text-muted">{ms(r.latency_ms)}</td>
                    <td className="whitespace-nowrap text-muted" title={dateTime(r.created_at)}>{relative(r.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </Table>
            {q.hasNextPage && (
              <div className="border-t border-line p-3 text-center">
                <Button onClick={() => q.fetchNextPage()} loading={q.isFetchingNextPage}>Daha fazla göster</Button>
              </div>
            )}
          </>
        )}
      </Panel>
      <DecisionModal publicId={open} onClose={() => setOpen(null)} />
    </>
  );
}

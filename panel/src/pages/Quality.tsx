import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api";
import { RANGE_OPTIONS } from "../components/Overview";
import { Empty, ErrorBox, PageHeader, Panel, Segmented, Select, Spinner, Stat, Table } from "../components/ui";
import { categoryLabel, ms, num, pct } from "../format";
import type { Project, RangeKey } from "../types";

type QualityData = {
  categories: { category: string; reviewed: number; human_block: number; human_allow: number }[];
  ai: { ai_calls: number; ai_avg_ms: number | null; ai_resolved: number; layer1_review: number };
};

function advice(block: number, total: number): { text: string; tone: string } {
  if (total < 20) return { text: "Karar için veri az (en az 20 inceleme)", tone: "text-faint" };
  const rate = block / total;
  if (rate >= 0.9) return { text: "Neredeyse hepsi engelleniyor: engelleme eşiğini düşürmeyi düşünün", tone: "text-block-ink" };
  if (rate <= 0.1) return { text: "Neredeyse hepsine izin veriliyor: inceleme eşiğini yükseltmeyi düşünün", tone: "text-review-ink" };
  return { text: "Dengeli: inceleme gerçekten belirsiz içeriği yakalıyor", tone: "text-allow-ink" };
}

export function QualityPage() {
  const [range, setRange] = useState<RangeKey>("30d");
  const [projectId, setProjectId] = useState("");
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/projects") });
  const q = useQuery({
    queryKey: ["quality", range, projectId],
    queryFn: () => api<QualityData>(`/stats/quality?range=${range}${projectId ? `&project_id=${projectId}` : ""}`),
  });

  return (
    <>
      <PageHeader title="Karar kalitesi"
        description="İncelemeye düşen içeriklerde moderatörlerin verdiği kararlar. Eşikleri tahminle değil bu veriyle ayarlayın."
        actions={<>
          <Select aria-label="Proje" value={projectId} onChange={(e) => setProjectId(e.target.value)} className="w-44">
            <option value="">Tüm projeler</option>
            {projects.data?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </Select>
          <Segmented label="Zaman aralığı" value={range} onChange={setRange} options={RANGE_OPTIONS} />
        </>} />
      {q.isLoading ? <Spinner /> : q.error ? <ErrorBox error={q.error} onRetry={() => q.refetch()} /> : (() => {
        const d = q.data!;
        return (
          <div className="space-y-6">
            <Panel title="AI katmanı (Layer 2)">
              <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
                <Stat label="AI çağrısı" value={num(d.ai.ai_calls)} />
                <Stat label="Ortalama süre" value={ms(d.ai.ai_avg_ms)} />
                <Stat label="Layer 1'in belirsiz bulduğu" value={num(d.ai.layer1_review)} />
                <Stat label="AI'nın çözdüğü" value={num(d.ai.ai_resolved)} hint={d.ai.layer1_review ? `${pct(d.ai.ai_resolved, d.ai.layer1_review)} daha az insan incelemesi` : undefined} />
              </div>
            </Panel>
            <Panel title="Kategoriye göre insan kararları" padded={false}>
              {d.categories.length === 0 ? <Empty title="Bu aralıkta incelenmiş içerik yok">İnceleme kuyruğunda karar verildikçe burada görünür.</Empty> : (
                <Table>
                  <thead><tr><th>Kategori</th><th>İncelenen</th><th>İnsan: engelle</th><th>İnsan: izin</th><th>Öneri</th></tr></thead>
                  <tbody>
                    {d.categories.map((c) => {
                      const a = advice(c.human_block, c.reviewed);
                      return (
                        <tr key={c.category}>
                          <td className="font-medium">{c.category === "none" ? "Kategorisiz" : categoryLabel(c.category)}</td>
                          <td>{num(c.reviewed)}</td>
                          <td>{num(c.human_block)} <span className="text-muted">({pct(c.human_block, c.reviewed)})</span></td>
                          <td>{num(c.human_allow)} <span className="text-muted">({pct(c.human_allow, c.reviewed)})</span></td>
                          <td className={`text-sm ${a.tone}`}>{a.text}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </Table>
              )}
            </Panel>
          </div>
        );
      })()}
    </>
  );
}

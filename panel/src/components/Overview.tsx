import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";

import { api } from "../api";
import { categoryLabel, ms, num, pct } from "../format";
import { useChartColors } from "../theme";
import type { Overview, RangeKey } from "../types";
import { ErrorBox, Panel, Spinner, Stat } from "./ui";
import { VerdictStrip } from "./VerdictStrip";

export const RANGE_OPTIONS: { value: RangeKey; label: string }[] = [
  { value: "24h", label: "24 saat" },
  { value: "7d", label: "7 gün" },
  { value: "30d", label: "30 gün" },
  { value: "90d", label: "90 gün" },
];
const RANGE_TEXT: Record<RangeKey, string> = { "24h": "Son 24 saat", "7d": "Son 7 gün", "30d": "Son 30 gün", "90d": "Son 90 gün" };


function tickFormatter(bucket: "hour" | "day", timeZone: string) {
  const f = new Intl.DateTimeFormat("tr-TR", bucket === "hour" ? { hour: "2-digit", minute: "2-digit", timeZone } : { day: "numeric", month: "short", timeZone });
  return (v: string) => f.format(new Date(v));
}

function ChartTooltip({ active, payload, label, bucket, timeZone }: {
  active?: boolean; payload?: { name: string; value: number; color: string }[]; label?: string; bucket: "hour" | "day"; timeZone: string;
}) {
  if (!active || !payload?.length || !label) return null;
  const f = new Intl.DateTimeFormat("tr-TR", bucket === "hour" ? { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone } : { dateStyle: "medium", timeZone });
  return (
    <div className="rounded-md border border-line bg-surface px-3 py-2 text-xs shadow-lg">
      <div className="mb-1 font-medium">{f.format(new Date(label))}</div>
      {payload.map((p) => (
        <div key={p.name} className="flex items-center justify-between gap-6">
          <span className="flex items-center gap-1.5 text-muted"><span className="size-2 rounded-full" style={{ background: p.color }} />{p.name}</span>
          <span className="font-medium">{p.name === "Ortalama" ? ms(p.value) : num(p.value)}</span>
        </div>
      ))}
    </div>
  );
}

export function useOverview(range: RangeKey, projectId?: string) {
  return useQuery({
    queryKey: ["overview", range, projectId ?? "all"],
    queryFn: () => api<Overview>(`/stats/overview?range=${range}${projectId ? `&project_id=${projectId}` : ""}`),
    refetchInterval: 30_000,
  });
}

export function OverviewView({ range, projectId }: { range: RangeKey; projectId?: string }) {
  const q = useOverview(range, projectId);
  const COLORS = useChartColors();
  if (q.isLoading) return <Spinner label="İstatistikler hesaplanıyor" />;
  if (q.error) return <ErrorBox error={q.error} onRetry={() => q.refetch()} />;
  const d = q.data!;
  const t = d.totals;
  const decided = t.allow + t.review + t.block;
  const fmt = tickFormatter(d.bucket, d.timezone);
  const maxCategory = Math.max(1, ...d.categories.map((c) => c.count));

  return (
    <div className="space-y-6">
      {/* Karar dağılımı: sayfanın ana öğesi */}
      <section className="rounded-lg border border-line bg-surface p-5 sm:p-6">
        <div className="flex flex-wrap items-end justify-between gap-6">
          <div>
            <div className="text-xs text-muted">{RANGE_TEXT[range]} içinde işlenen içerik</div>
            <div className="mt-1 text-4xl font-semibold leading-none tabular-nums">{num(t.requests)}</div>
          </div>
          <div className="flex flex-wrap gap-x-8 gap-y-3">
            <Stat label="Son 1 dakika" value={num(d.live.last_minute)} />
            <Stat label="Bugün" value={num(d.live.today)} />
            <Stat
              label="İnceleme bekleyen"
              value={d.pending_reviews > 0 ? <Link to="/review" className="hover:underline">{num(d.pending_reviews)}</Link> : "0"}
              tone={d.pending_reviews > 0 ? "review" : undefined}
            />
            <Stat label="Hata" value={num(t.failed)} tone={t.failed > 0 ? "block" : undefined} hint={t.in_progress ? `${num(t.in_progress)} işleniyor` : undefined} />
          </div>
        </div>
        <div className="mt-6">
          <VerdictStrip counts={t} size="lg" legend animate />
        </div>
      </section>

      <div className="grid gap-6 xl:grid-cols-3">
        <Panel className="xl:col-span-2" title="Zaman içinde kararlar" description={d.bucket === "hour" ? "Saatlik" : "Günlük"}>
          {decided === 0 && t.failed === 0 ? (
            <p className="py-16 text-center text-sm text-muted">Bu aralıkta henüz istek yok.</p>
          ) : (
            <div className="h-64">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={d.series} margin={{ top: 4, right: 4, left: -12, bottom: 0 }} barCategoryGap="18%">
                  <CartesianGrid vertical={false} stroke={COLORS.line} strokeDasharray="3 3" />
                  <XAxis dataKey="t" tickFormatter={fmt} tickLine={false} axisLine={false} minTickGap={24} />
                  <YAxis tickLine={false} axisLine={false} allowDecimals={false} tickFormatter={(v) => num(v)} />
                  <Tooltip cursor={{ fill: COLORS.cursor }} content={<ChartTooltip bucket={d.bucket} timeZone={d.timezone} />} />
                  <Bar dataKey="allow" name="İzin" stackId="d" fill={COLORS.allow} />
                  <Bar dataKey="review" name="İncele" stackId="d" fill={COLORS.review} />
                  <Bar dataKey="block" name="Engelle" stackId="d" fill={COLORS.block} radius={[2, 2, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
        </Panel>

        <Panel title="Yanıt süresi" description="İsteğin gelişinden kararın hazır olmasına kadar">
          <div className="grid grid-cols-4 gap-2">
            <Stat label="Ort." value={ms(d.latency.avg)} />
            <Stat label="P50" value={ms(d.latency.p50)} />
            <Stat label="P95" value={ms(d.latency.p95)} />
            <Stat label="P99" value={ms(d.latency.p99)} />
          </div>
          <div className="mt-4 h-36">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={d.series} margin={{ top: 4, right: 4, left: -12, bottom: 0 }}>
                <CartesianGrid vertical={false} stroke={COLORS.line} strokeDasharray="3 3" />
                <XAxis dataKey="t" tickFormatter={fmt} tickLine={false} axisLine={false} minTickGap={32} />
                <YAxis tickLine={false} axisLine={false} tickFormatter={(v) => (v >= 1000 ? `${v / 1000}s` : `${v}`)} />
                <Tooltip content={<ChartTooltip bucket={d.bucket} timeZone={d.timezone} />} />
                <Line dataKey="latency" name="Ortalama" stroke={COLORS.primary} strokeWidth={2} dot={false} connectNulls />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </Panel>
      </div>

      <div className={`grid gap-6 ${projectId ? "lg:grid-cols-2" : "lg:grid-cols-3"}`}>
        <Panel title="Tespit edilen kategoriler" description="Bir içerikte birden fazla kategori olabilir">
          {d.categories.length === 0 ? (
            <p className="py-6 text-sm text-muted">Bu aralıkta ihlal tespit edilmedi.</p>
          ) : (
            <ul className="space-y-3">
              {d.categories.map((c) => (
                <li key={c.name}>
                  <div className="mb-1 flex justify-between text-[13px]"><span>{categoryLabel(c.name)}</span><span className="text-muted">{num(c.count)}</span></div>
                  <div className="h-1.5 rounded-full bg-sunken"><div className="h-full rounded-full bg-primary" style={{ width: `${(c.count / maxCategory) * 100}%` }} /></div>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="İçerik türleri">
          <ul className="space-y-3 text-sm">
            {([["text", "Metin"], ["image", "Görsel"], ["video", "Video"]] as const).map(([k, label]) => (
              <li key={k} className="flex items-center justify-between">
                <span>{label}</span>
                <span className="text-muted"><span className="font-medium text-ink">{num(t[k])}</span> · {pct(t[k], t.requests)}</span>
              </li>
            ))}
          </ul>
        </Panel>

        {!projectId && (
          <Panel title="En çok istek gönderen projeler" padded={false}>
            {d.projects.length === 0 ? (
              <p className="p-5 text-sm text-muted">Bu aralıkta istek yok.</p>
            ) : (
              <ul>
                {d.projects.map((p) => (
                  <li key={p.id} className="border-t border-line px-5 py-3 first:border-t-0">
                    <div className="mb-1.5 flex items-center justify-between text-sm">
                      <Link to={`/projects/${p.id}`} className="font-medium hover:underline">{p.name}</Link>
                      <span className="text-muted">{num(p.requests)}</span>
                    </div>
                    <VerdictStrip counts={{ allow: Math.max(0, p.requests - p.block - p.review), review: p.review, block: p.block }} />
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        )}
      </div>
    </div>
  );
}

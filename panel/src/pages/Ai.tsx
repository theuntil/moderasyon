import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { api, ApiError, del, patch, post } from "../api";
import { useAuth } from "../auth";
import { useToast } from "../components/toast";
import {
  Button, ErrorBox, Field, Input, Mono, PageHeader, Panel, Select, Spinner, Stat, Switch, Table, Tag,
} from "../components/ui";
import { can, dateTime, ms, num, relative } from "../format";
import { useChartColors } from "../theme";

type Num = number | string;
type Overview = {
  settings: {
    ai_enabled: boolean; ai_provider: "openai_moderation" | "chat"; ai_model: string; ai_vision_model: string;
    ai_max_calls_per_minute: number; ai_daily_budget_usd: Num; ai_monthly_budget_usd: Num; ai_alert_percent: number;
  };
  configured: boolean; base_url: string | null; api_key: string | null; moderation_model: string; circuit: string | null;
  totals: {
    today: string; today_cost: Num; today_reserved: Num; month_cost: Num; today_calls: number; today_errors: number;
    today_skipped: number; month_calls: number; month_tokens: number; today_avg_latency: number | null;
  };
  series: { day: string; cost: Num; calls: number; errors: number; skipped: number }[];
  by_project: { project_id: string; name: string; budget: Num | null; calls: number; cost: Num; skipped: number }[];
  by_model: { model: string; calls: number; errors: number; input_tokens: number; output_tokens: number; cost: Num }[];
  recent: { id: number; project_name: string | null; model: string; kind: string; status: string; input_tokens: number;
    output_tokens: number; cost_usd: Num; latency_ms: number | null; error: string | null; created_at: string }[];
  alerts: { id: number; kind: string; message: string; created_at: string }[];
};
type Price = { model: string; input_per_1m: Num; output_per_1m: Num; updated_at: string; updated_by: string | null };

const n = (v: Num | null | undefined) => (v == null ? 0 : Number(v));
export const usd = (v: Num | null | undefined, digits = 4) =>
  `$${new Intl.NumberFormat("tr-TR", { minimumFractionDigits: 2, maximumFractionDigits: digits }).format(n(v))}`;

const STATUS_LABEL: Record<string, [string, "good" | "warn" | "bad" | "neutral"]> = {
  ok: ["Başarılı", "good"], error: ["Hata", "bad"], timeout: ["Zaman aşımı", "bad"], rate_limited: ["Hız sınırı (429)", "warn"],
  auth_error: ["Anahtar reddedildi", "bad"], bad_response: ["Bozuk yanıt", "bad"],
};

function BudgetBar({ label, spent, reserved = 0, budget }: { label: string; spent: number; reserved?: number; budget: number }) {
  const pct = budget > 0 ? Math.min(100, ((spent + reserved) / budget) * 100) : spent > 0 ? 100 : 0;
  const tone = pct >= 100 ? "bg-block" : pct >= 80 ? "bg-review" : "bg-allow";
  return (
    <div>
      <div className="flex items-baseline justify-between text-sm">
        <span className="text-muted">{label}</span>
        <span><strong className="font-semibold">{usd(spent)}</strong> <span className="text-muted">/ {budget > 0 ? usd(budget) : "kapalı"}</span></span>
      </div>
      <div className="mt-1.5 h-2 overflow-hidden rounded-full bg-sunken"><div className={`h-full ${tone}`} style={{ width: `${pct}%` }} /></div>
      {budget === 0 && <p className="mt-1 text-xs text-muted">Bütçe 0: ücretli modellerle çağrı yapılmaz (ücretsiz Moderation API çalışır).</p>}
    </div>
  );
}

function SettingsForm({ d, prices }: { d: Overview; prices: Price[] }) {
  const qc = useQueryClient();
  const toast = useToast();
  const { user } = useAuth();
  const isAdmin = can(user?.role, "admin");
  const [f, setF] = useState(d.settings);
  useEffect(() => setF(d.settings), [d.settings]);
  const set = <K extends keyof Overview["settings"]>(k: K, v: Overview["settings"][K]) => setF((p) => ({ ...p, [k]: v }));

  const save = useMutation({
    mutationFn: (body: object) => patch("/ai/settings", body),
    onSuccess: () => { toast("success", "AI ayarları kaydedildi. En geç 5 saniyede tüm işçilere yansır."); qc.invalidateQueries({ queryKey: ["ai"] }); },
    onError: (e) => toast("error", (e as ApiError).message),
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    save.mutate({
      ai_provider: f.ai_provider, ai_model: f.ai_model, ai_vision_model: f.ai_vision_model,
      ai_max_calls_per_minute: Number(f.ai_max_calls_per_minute), ai_daily_budget_usd: Number(f.ai_daily_budget_usd),
      ai_monthly_budget_usd: Number(f.ai_monthly_budget_usd), ai_alert_percent: Number(f.ai_alert_percent),
    });
  };
  const paidModels = prices.filter((p) => p.model !== d.moderation_model);

  return (
    <Panel title="Ayarlar" description="API anahtarı güvenlik nedeniyle sunucu ortam değişkeninde (AI_API_KEY) durur."
      actions={<div className="flex items-center gap-2 text-sm">
        <span className="text-muted">{f.ai_enabled ? "AI açık" : "AI kapalı"}</span>
        <Switch label="AI genel anahtarı" checked={d.settings.ai_enabled} disabled={!isAdmin}
          onChange={(v) => save.mutate({ ai_enabled: v })} />
      </div>}>
      <form onSubmit={submit} className="grid gap-4 md:grid-cols-2">
        <Field label="Sağlayıcı" hint={f.ai_provider === "openai_moderation"
          ? "Ücretsiz. Metin: taciz, nefret, cinsel, şiddet, kendine zarar, yasa dışı. Görsel: cinsel, şiddet, kendine zarar."
          : "Token başına ücretli. Silah, uyuşturucu, spam ve dolandırıcılık dahil tüm kategoriler; Türkçe bağlamı daha iyi anlar."}>
          {(id) => (
            <Select id={id} value={f.ai_provider} disabled={!isAdmin} onChange={(e) => set("ai_provider", e.target.value as Overview["settings"]["ai_provider"])}>
              <option value="openai_moderation">OpenAI Moderation (ücretsiz)</option>
              <option value="chat">Sohbet modeli (ücretli)</option>
            </Select>
          )}
        </Field>
        {f.ai_provider === "chat" ? (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 md:col-span-2 md:order-none">
            <Field label="Metin modeli">
              {(id) => <Select id={id} value={f.ai_model} disabled={!isAdmin} onChange={(e) => set("ai_model", e.target.value)}>
                {paidModels.map((p) => <option key={p.model} value={p.model}>{p.model}</option>)}
              </Select>}
            </Field>
            <Field label="Görsel modeli">
              {(id) => <Select id={id} value={f.ai_vision_model} disabled={!isAdmin} onChange={(e) => set("ai_vision_model", e.target.value)}>
                {paidModels.map((p) => <option key={p.model} value={p.model}>{p.model}</option>)}
              </Select>}
            </Field>
          </div>
        ) : (
          <Field label="Model">{(id) => <Input id={id} value={d.moderation_model} disabled className="font-mono" />}</Field>
        )}
        <Field label="Günlük bütçe (USD)" hint="Aşılamaz: tahmini maliyet çağrıdan önce ayrılır.">
          {(id) => <Input id={id} type="number" step="0.01" min="0" disabled={!isAdmin} value={String(f.ai_daily_budget_usd)} onChange={(e) => set("ai_daily_budget_usd", e.target.value)} />}
        </Field>
        <Field label="Aylık bütçe (USD)">
          {(id) => <Input id={id} type="number" step="0.01" min="0" disabled={!isAdmin} value={String(f.ai_monthly_budget_usd)} onChange={(e) => set("ai_monthly_budget_usd", e.target.value)} />}
        </Field>
        <Field label="Dakikalık çağrı sınırı" hint="0 = sınırsız. OpenAI hız sınırına ve ani maliyet artışına karşı sigorta.">
          {(id) => <Input id={id} type="number" min="0" disabled={!isAdmin} value={String(f.ai_max_calls_per_minute)} onChange={(e) => set("ai_max_calls_per_minute", Number(e.target.value))} />}
        </Field>
        <Field label="Uyarı eşiği (%)" hint="Bütçenin bu kadarı kullanılınca panelde ve denetim kaydında uyarı.">
          {(id) => <Input id={id} type="number" min="1" max="100" disabled={!isAdmin} value={String(f.ai_alert_percent)} onChange={(e) => set("ai_alert_percent", Number(e.target.value))} />}
        </Field>
        {isAdmin && <div className="md:col-span-2"><Button type="submit" variant="primary" loading={save.isPending}>Kaydet</Button></div>}
      </form>
    </Panel>
  );
}

function Prices({ prices, active }: { prices: Price[]; active: string[] }) {
  const qc = useQueryClient();
  const toast = useToast();
  const { user } = useAuth();
  const isAdmin = can(user?.role, "admin");
  const [model, setModel] = useState("");
  const [pin, setPin] = useState("");
  const [pout, setPout] = useState("");
  const refresh = () => qc.invalidateQueries({ queryKey: ["ai-prices"] });
  const put = useMutation({
    mutationFn: (b: { model: string; i: string; o: string }) => api(`/ai/prices/${encodeURIComponent(b.model)}`, { method: "PUT", body: { input_per_1m: Number(b.i), output_per_1m: Number(b.o) } }),
    onSuccess: () => { toast("success", "Fiyat kaydedildi."); setModel(""); setPin(""); setPout(""); refresh(); },
    onError: (e) => toast("error", (e as ApiError).message),
  });
  const remove = useMutation({
    mutationFn: (m: string) => del(`/ai/prices/${encodeURIComponent(m)}`),
    onSuccess: () => { toast("success", "Fiyat silindi."); refresh(); },
    onError: (e) => toast("error", (e as ApiError).message),
  });
  return (
    <Panel title="Model fiyatları" padded={false} description="USD / 1 milyon token. OpenAI fiyat değiştirirse buradan güncelleyin; fiyatı olmayan ücretli modelle çağrı yapılmaz.">
      <Table>
        <thead><tr><th>Model</th><th>Girdi</th><th>Çıktı</th><th>Güncelleme</th><th /></tr></thead>
        <tbody>
          {prices.map((p) => (
            <tr key={p.model}>
              <td><Mono>{p.model}</Mono> {active.includes(p.model) && <Tag tone="info">Kullanımda</Tag>}</td>
              <td>{n(p.input_per_1m) === 0 ? "Ücretsiz" : usd(p.input_per_1m, 4)}</td>
              <td>{n(p.output_per_1m) === 0 ? "Ücretsiz" : usd(p.output_per_1m, 4)}</td>
              <td className="text-muted">{p.updated_by ?? "kurulum"} · {relative(p.updated_at)}</td>
              <td className="text-right">{isAdmin && !active.includes(p.model) && <Button size="sm" variant="ghostDanger" onClick={() => remove.mutate(p.model)}>Sil</Button>}</td>
            </tr>
          ))}
        </tbody>
      </Table>
      {isAdmin && (
        <form onSubmit={(e) => { e.preventDefault(); put.mutate({ model: model.trim(), i: pin, o: pout }); }}
          className="flex flex-wrap items-end gap-2 border-t border-line p-4">
          <Field label="Model adı">{(id) => <Input id={id} required value={model} onChange={(e) => setModel(e.target.value)} placeholder="gpt-5-nano" className="w-48 font-mono" />}</Field>
          <Field label="Girdi $/1M">{(id) => <Input id={id} required type="number" step="0.000001" min="0" value={pin} onChange={(e) => setPin(e.target.value)} className="w-28" />}</Field>
          <Field label="Çıktı $/1M">{(id) => <Input id={id} required type="number" step="0.000001" min="0" value={pout} onChange={(e) => setPout(e.target.value)} className="w-28" />}</Field>
          <Button type="submit" loading={put.isPending}>Ekle / güncelle</Button>
        </form>
      )}
    </Panel>
  );
}

export function AiPage() {
  const { user } = useAuth();
  const isAdmin = can(user?.role, "admin");
  const toast = useToast();
  const qc = useQueryClient();
  const colors = useChartColors();
  const q = useQuery({ queryKey: ["ai"], queryFn: () => api<Overview>("/ai/overview"), refetchInterval: 20_000 });
  const prices = useQuery({ queryKey: ["ai-prices"], queryFn: () => api<Price[]>("/ai/prices") });
  const test = useMutation({
    mutationFn: () => post<{ status: string; model: string; latency_ms: number | null; cost_usd: Num; top_scores: { name: string; score: number }[] }>("/ai/test", {}),
    onSuccess: (r) => {
      if (r.status === "ok") toast("success", `Bağlantı çalışıyor: ${r.model}, ${ms(r.latency_ms)}, maliyet ${usd(r.cost_usd, 6)}.`);
      else toast("error", `Test başarısız: ${r.status}. Son çağrılar tablosunda ayrıntı var.`);
      qc.invalidateQueries({ queryKey: ["ai"] });
    },
    onError: (e) => toast("error", (e as ApiError).message),
  });
  const reset = useMutation({
    mutationFn: () => post("/ai/circuit/reset"),
    onSuccess: () => { toast("success", "Devre kesici sıfırlandı."); qc.invalidateQueries({ queryKey: ["ai"] }); },
  });

  if (q.isLoading || prices.isLoading) return <Spinner />;
  if (q.error) return <ErrorBox error={q.error} onRetry={() => q.refetch()} />;
  const d = q.data!;
  const t = d.totals;
  const active = d.settings.ai_provider === "chat" ? [d.settings.ai_model, d.settings.ai_vision_model] : [d.moderation_model];
  const status = !d.configured ? ["Anahtar tanımlı değil", "warn"] : !d.settings.ai_enabled ? ["Kapalı", "neutral"]
    : d.circuit ? [d.circuit === "auth_error" ? "Anahtar reddedildi, duraklatıldı" : "Hatalar nedeniyle duraklatıldı", "bad"] : ["Çalışıyor", "good"];
  const fmtDay = new Intl.DateTimeFormat("tr-TR", { day: "numeric", month: "short", timeZone: "UTC" });

  return (
    <>
      <PageHeader title="AI ve maliyet" description="Layer 2 sağlayıcısı, bütçeler ve harcama. AI yanıt veremezse veya bütçe dolarsa yerel modeller karar verir; içerik asla sessizce onaylanmaz."
        actions={isAdmin && <>
          {d.circuit && <Button onClick={() => reset.mutate()} loading={reset.isPending}>Devre kesiciyi sıfırla</Button>}
          <Button variant="primary" onClick={() => test.mutate()} loading={test.isPending} disabled={!d.configured}>Bağlantıyı test et</Button>
        </>} />

      <div className="space-y-6">
        <section className="grid gap-4 lg:grid-cols-3">
          <Panel title="Durum">
            <div className="space-y-2 text-sm">
              <div className="flex justify-between"><span className="text-muted">Durum</span><Tag tone={status[1] as "good"}>{status[0]}</Tag></div>
              <div className="flex justify-between"><span className="text-muted">Sağlayıcı</span><span>{d.settings.ai_provider === "openai_moderation" ? "OpenAI Moderation" : "Sohbet modeli"}</span></div>
              <div className="flex justify-between"><span className="text-muted">Model</span><Mono>{active[0]}</Mono></div>
              <div className="flex justify-between"><span className="text-muted">Anahtar</span><Mono>{d.api_key ?? "—"}</Mono></div>
              <div className="flex justify-between"><span className="text-muted">Uç nokta</span><Mono className="truncate">{d.base_url ?? "—"}</Mono></div>
            </div>
          </Panel>
          <Panel title="Bütçe" className="lg:col-span-2">
            <div className="space-y-4">
              <BudgetBar label="Bugün" spent={n(t.today_cost)} reserved={n(t.today_reserved)} budget={n(d.settings.ai_daily_budget_usd)} />
              <BudgetBar label="Bu ay" spent={n(t.month_cost)} budget={n(d.settings.ai_monthly_budget_usd)} />
              <div className="grid grid-cols-2 gap-3 border-t border-line pt-4 sm:grid-cols-4">
                <Stat label="Bugün çağrı" value={num(t.today_calls)} />
                <Stat label="Hata" value={num(t.today_errors)} tone={t.today_errors ? "block" : undefined} />
                <Stat label="Atlanan" value={num(t.today_skipped)} hint="bütçe/limit/devre" tone={t.today_skipped ? "review" : undefined} />
                <Stat label="Ort. süre" value={ms(t.today_avg_latency)} />
              </div>
            </div>
          </Panel>
        </section>

        {d.alerts.length > 0 && (
          <Panel title="Uyarılar" padded={false}>
            <ul>{d.alerts.slice(0, 6).map((a) => (
              <li key={a.id} className="flex items-start justify-between gap-4 border-t border-line px-5 py-2.5 text-sm first:border-t-0">
                <span className={a.kind.endsWith("exhausted") || a.kind === "auth_error" ? "text-block-ink" : "text-review-ink"}>{a.message}</span>
                <span className="shrink-0 text-xs text-muted">{relative(a.created_at)}</span>
              </li>))}
            </ul>
          </Panel>
        )}

        <Panel title="Son 30 gün" description={`Bu ay ${num(t.month_calls)} çağrı, ${num(t.month_tokens)} token`}>
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={d.series.map((s) => ({ ...s, cost: n(s.cost) }))} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                <CartesianGrid vertical={false} strokeDasharray="3 3" />
                <XAxis dataKey="day" tickFormatter={(v) => fmtDay.format(new Date(v))} tickLine={false} axisLine={false} minTickGap={24} />
                <YAxis tickLine={false} axisLine={false} tickFormatter={(v) => `$${v}`} width={56} />
                <Tooltip cursor={{ fill: colors.cursor }} content={({ active: a, payload }) => a && payload?.length ? (
                  <div className="rounded-md border border-line bg-surface px-3 py-2 text-xs shadow-lg">
                    <div className="font-medium">{fmtDay.format(new Date(payload[0].payload.day))}</div>
                    <div>{usd(payload[0].payload.cost, 6)} · {num(payload[0].payload.calls)} çağrı · {num(payload[0].payload.skipped)} atlanan</div>
                  </div>) : null} />
                <Bar dataKey="cost" fill={colors.primary} radius={[2, 2, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Panel>

        <div className="grid gap-6 lg:grid-cols-2">
          <SettingsForm d={d} prices={prices.data ?? []} />
          <Prices prices={prices.data ?? []} active={active} />
        </div>

        <div className="grid gap-6 lg:grid-cols-2">
          <Panel title="Bu ay projeler" padded={false}>
            <Table>
              <thead><tr><th>Proje</th><th>Çağrı</th><th>Maliyet</th><th>Proje bütçesi</th></tr></thead>
              <tbody>{d.by_project.length === 0 ? <tr><td colSpan={4} className="text-muted">Henüz AI kullanımı yok.</td></tr> : d.by_project.map((p) => (
                <tr key={p.project_id}><td>{p.name}</td><td>{num(p.calls)}{p.skipped ? <span className="text-xs text-muted"> (+{p.skipped} atlanan)</span> : null}</td>
                  <td>{usd(p.cost)}</td><td>{p.budget == null ? <span className="text-muted">Sınırsız</span> : usd(p.budget)}</td></tr>))}
              </tbody>
            </Table>
          </Panel>
          <Panel title="Bu ay modeller" padded={false}>
            <Table>
              <thead><tr><th>Model</th><th>Çağrı</th><th>Token (girdi/çıktı)</th><th>Maliyet</th></tr></thead>
              <tbody>{d.by_model.length === 0 ? <tr><td colSpan={4} className="text-muted">Henüz AI kullanımı yok.</td></tr> : d.by_model.map((m) => (
                <tr key={m.model}><td><Mono>{m.model}</Mono></td><td>{num(m.calls)}{m.errors ? <span className="text-xs text-block-ink"> ({m.errors} hata)</span> : null}</td>
                  <td>{num(m.input_tokens)} / {num(m.output_tokens)}</td><td>{usd(m.cost)}</td></tr>))}
              </tbody>
            </Table>
          </Panel>
        </div>

        <Panel title="Son çağrılar" padded={false}>
          <Table>
            <thead><tr><th>Zaman</th><th>Proje</th><th>Model</th><th>Tür</th><th>Durum</th><th>Token</th><th>Maliyet</th><th>Süre</th></tr></thead>
            <tbody>{d.recent.length === 0 ? <tr><td colSpan={8} className="text-muted">Henüz çağrı yok.</td></tr> : d.recent.map((c) => {
              const [label, tone] = STATUS_LABEL[c.status] ?? [c.status, "neutral"];
              return (
                <tr key={c.id}>
                  <td className="whitespace-nowrap text-muted" title={dateTime(c.created_at)}>{relative(c.created_at)}</td>
                  <td>{c.project_name ?? "Panel testi"}</td><td><Mono>{c.model}</Mono></td>
                  <td>{c.kind === "image" ? "Görsel" : "Metin"}</td>
                  <td><Tag tone={tone}>{label}</Tag>{c.error && <div className="max-w-[220px] truncate text-xs text-muted" title={c.error}>{c.error}</div>}</td>
                  <td>{num(c.input_tokens + c.output_tokens)}</td><td>{usd(c.cost_usd, 6)}</td><td>{ms(c.latency_ms)}</td>
                </tr>);
            })}</tbody>
          </Table>
        </Panel>
      </div>
    </>
  );
}

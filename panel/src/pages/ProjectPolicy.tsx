import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api, ApiError } from "../api";
import { useAuth } from "../auth";
import { useToast } from "../components/toast";
import { Button, ErrorBox, Input, Panel, Segmented, Select, Spinner, Table, Tag } from "../components/ui";
import { can, categoryLabel } from "../format";
import type { PlatformSettings } from "../types";

type Rule = { action: "default" | "allow" | "review" | "block"; review?: number; block?: number };
type PolicyResponse = {
  policy: {
    categories?: Record<string, Rule>; ai_mode?: "off" | "smart" | "always"; decision_mode?: "two_step" | "three_step";
    uncertain_action?: "block" | "allow"; ai_trigger?: number; fallback_decision?: "allow" | "review" | "block" | null;
    profanity_level?: "strict" | "moderate" | "off";
  };
  revision: number;
  categories: string[];
  ai_available: boolean;
  ai_provider?: string;
  ai_monthly_budget_usd?: number | string | null;
};

const ACTION_LABEL: Record<Rule["action"], string> = {
  default: "Eşiklere göre",
  allow: "Yok say",
  review: "En fazla incele",
  block: "Sıkı: engelle",
};

const AI_HELP: Record<string, string> = {
  off: "Sadece yerel modeller karar verir.",
  smart: "Risk skoru eşiği geçen ve kesin engellenmemiş içerik AI'ya sorulur; AI izin mi engel mi olduğuna karar verir.",
  always: "Kesin engellenmemiş her içerik AI'dan da geçer; AI yeni tespit ekler (şiddet, kendine zarar...).",
};

export function PolicyTab({ projectId }: { projectId: string }) {
  const { user } = useAuth();
  const isAdmin = can(user?.role, "admin");
  const toast = useToast();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["policy", projectId], queryFn: () => api<PolicyResponse>(`/projects/${projectId}/policy`) });
  const platform = useQuery({ queryKey: ["settings"], queryFn: () => api<PlatformSettings>("/settings") });
  const [rules, setRules] = useState<Record<string, Rule>>({});
  const [aiMode, setAiMode] = useState<"off" | "smart" | "always">("smart");
  const [mode, setMode] = useState<"two_step" | "three_step">("two_step");
  const [uncertain, setUncertain] = useState<"block" | "allow">("block");
  const [trigger, setTrigger] = useState("0.5");
  const [fallback, setFallback] = useState<string>("");
  const [profanity, setProfanity] = useState<"strict" | "moderate" | "off">("strict");
  const [budget, setBudget] = useState("");

  useEffect(() => {
    if (!q.data) return;
    setRules(q.data.policy.categories ?? {});
    setAiMode(q.data.policy.ai_mode ?? "smart");
    setMode(q.data.policy.decision_mode ?? "two_step");
    setUncertain(q.data.policy.uncertain_action ?? "block");
    setTrigger(String(q.data.policy.ai_trigger ?? 0.5));
    setFallback(q.data.policy.fallback_decision ?? "");
    setProfanity(q.data.policy.profanity_level ?? "strict");
    setBudget(q.data.ai_monthly_budget_usd == null ? "" : String(Number(q.data.ai_monthly_budget_usd)));
  }, [q.data]);

  const save = useMutation({
    mutationFn: async () => {
      await api(`/projects/${projectId}/ai-budget`, { method: "PUT", body: { monthly_budget_usd: budget === "" ? null : Number(budget) } });
      return api<{ revision: number }>(`/projects/${projectId}/policy`, { method: "PUT", body: {
        categories: rules, ai_mode: aiMode, decision_mode: mode, uncertain_action: uncertain,
        ai_trigger: Number(trigger), fallback_decision: fallback === "" || (mode === "two_step" && fallback === "review") ? null : fallback,
        profanity_level: profanity,
      } });
    },
    onSuccess: (r) => { toast("success", `Policy kaydedildi (revizyon ${r.revision}). Yeni içeriklere birkaç saniye içinde uygulanır.`); qc.invalidateQueries({ queryKey: ["policy", projectId] }); },
    onError: (e) => toast("error", (e as ApiError).message),
  });

  if (q.isLoading) return <Spinner />;
  if (q.error) return <ErrorBox error={q.error} onRetry={() => q.refetch()} />;
  const d = q.data!;
  const setRule = (name: string, patch: Partial<Rule>) =>
    setRules((prev) => {
      const next = Object.assign({ action: "default" }, prev[name], patch) as Rule;
      for (const k of ["review", "block"] as const) if (next[k] === undefined || Number.isNaN(next[k])) delete next[k];
      const copy = { ...prev };
      if (next.action === "default" && next.review === undefined && next.block === undefined) delete copy[name];
      else copy[name] = next;
      return copy;
    });
  const num = (v: string) => (v === "" ? undefined : Number(v));

  return (
    <div className="space-y-6">
      <Panel title="Karar akışı" actions={isAdmin && <Button variant="primary" size="sm" loading={save.isPending} onClick={() => save.mutate()}>Kaydet</Button>}>
        <div className="grid gap-5 lg:grid-cols-2">
          <div className="space-y-2">
            <div className="text-sm font-medium">Karar modu</div>
            <Segmented label="Karar modu" value={mode} onChange={setMode}
              options={[{ value: "two_step", label: "İki adım: izin / engel" }, { value: "three_step", label: "Üç adım: + incele" }]} />
            <p className="text-xs text-muted">{mode === "two_step"
              ? "Uygulamaya sadece izin ver veya engelle döner. Belirsiz içerik AI'ya sorulur; AI yoksa aşağıdaki kural uygulanır."
              : "Belirsiz içerik (AI'dan sonra da belirsizse) inceleme kuyruğuna düşer ve moderatör karar verir."}</p>
            {mode === "two_step" && (
              <label className="flex flex-wrap items-center gap-2 pt-1 text-sm">
                AI karar veremezse belirsiz içeriği
                <Select aria-label="Belirsiz içerik" className="h-8 w-36" disabled={!isAdmin} value={uncertain} onChange={(e) => setUncertain(e.target.value as "block" | "allow")}>
                  <option value="block">engelle</option>
                  <option value="allow">izin ver</option>
                </Select>
              </label>
            )}
          </div>
          <div className="space-y-2">
            <div className="text-sm font-medium">İşlenemezse (yedek karar)</div>
            <Select aria-label="Yedek karar" className="w-80 max-w-full" disabled={!isAdmin} value={fallback} onChange={(e) => setFallback(e.target.value)}>
              <option value="">Öneri yok (uygulama karar versin)</option>
              <option value="allow">İzin ver</option>
              <option value="block">Engelle</option>
              {mode === "three_step" && <option value="review">İncele</option>}
            </Select>
            <p className="text-xs text-muted">Sistem içeriği işleyemezse (kesinti, zaman aşımı) yanıtta <code>fallback_decision</code> olarak döner. Yanıtta her zaman <code>status</code> ve <code>retryable</code> da bulunur.</p>
          </div>
        </div>
      </Panel>

      <Panel title="Küfür seviyesi" description={{
          strict: "Sıkı: ağır küfür ve sokak ağzı (amk, mk, aq) engellenir; hafif hakaret (salak, aptal) belirsiz sayılır.",
          moderate: "Orta: sadece ağır küfür (siktir, orospu…) engellenir; sokak ağzı ve hafif hakaret serbest.",
          off: "Kapalı: küfür filtresi çalışmaz. Nefret söylemi, tehdit, müstehcenlik ve yasaklı kelimeler yine engellenir.",
        }[profanity]}
        actions={<Segmented label="Küfür seviyesi" value={profanity} onChange={setProfanity}
          options={[{ value: "strict", label: "Sıkı" }, { value: "moderate", label: "Orta" }, { value: "off", label: "Kapalı" }]} />}>
        <div className="grid gap-2 text-xs sm:grid-cols-3">
          {[["amk, mk, aq", { strict: "engel", moderate: "serbest", off: "serbest" }], ["siktir, orospu, piç", { strict: "engel", moderate: "engel", off: "serbest" }],
            ["salak, aptal", { strict: "belirsiz", moderate: "serbest", off: "serbest" }]].map(([w, m]) => (
            <div key={w as string} className="rounded-md border border-line px-3 py-2"><span className="text-muted">{w as string}</span> → <strong>{(m as Record<string, string>)[profanity]}</strong></div>
          ))}
        </div>
      </Panel>

      <Panel title="AI katmanı (Layer 2)" description={AI_HELP[aiMode]}
        actions={<Segmented label="AI modu" value={aiMode} onChange={setAiMode}
          options={[{ value: "off", label: "Kapalı" }, { value: "smart", label: "Risk eşiğini geçince" }, { value: "always", label: "Her zaman" }]} />}>
        {aiMode === "smart" && (
          <label className="mb-4 flex flex-wrap items-center gap-2 text-sm">
            Risk skoru
            <Input aria-label="AI tetikleme eşiği" type="number" step="0.05" min="0.05" max="0.99" className="h-8 w-24" disabled={!isAdmin}
              value={trigger} onChange={(e) => setTrigger(e.target.value)} />
            ve üstü olan içerik AI'ya sorulsun <span className="text-xs text-muted">(0,50 = %50)</span>
          </label>
        )}
        {d.ai_available ? (
          <p className="text-sm text-muted">Sağlayıcı: <strong className="text-ink">{d.ai_provider}</strong>. Yanıt vermezse veya bütçe dolarsa yerel modellerin kararı geçerli olur; içerik asla sessizce onaylanmaz. Çıplaklık şüphesi olan görseller dış sağlayıcıya gönderilmez.</p>
        ) : (
          <p className="rounded-md bg-review-soft px-3 py-2 text-sm text-review-ink">
            AI yapılandırılmamış veya kapalı. Mod seçebilirsiniz ama açılana kadar sadece yerel modeller çalışır.
          </p>
        )}
        <div className="mt-4 flex flex-wrap items-end gap-3 border-t border-line pt-4">
          <label className="flex flex-col gap-1.5 text-sm font-medium">
            Bu projenin aylık AI bütçesi (USD)
            <Input type="number" step="0.01" min="0" className="w-40" disabled={!isAdmin} value={budget}
              onChange={(e) => setBudget(e.target.value)} placeholder="Sınırsız" />
          </label>
          <p className="pb-2 text-xs text-muted">Boş: sadece platform bütçesi geçerli. Dolunca bu proje ay sonuna kadar yerel modellerle devam eder.</p>
          {isAdmin && <Button size="sm" variant="primary" loading={save.isPending} onClick={() => save.mutate()}>Kaydet</Button>}
        </div>
      </Panel>

      <Panel title="Kategori kuralları" padded={false}
        description={`Boş eşik platform varsayılanını kullanır (inceleme ${platform.data?.threshold_review ?? "…"}, engelleme ${platform.data?.threshold_block ?? "…"}). Revizyon ${d.revision}.`}
        actions={isAdmin && <Button variant="primary" size="sm" loading={save.isPending} onClick={() => save.mutate()}>Kaydet</Button>}>
        <Table>
          <thead><tr><th>Kategori</th><th>Davranış</th><th>İnceleme eşiği</th><th>Engelleme eşiği</th><th /></tr></thead>
          <tbody>
            {d.categories.map((name) => {
              const r = Object.assign({ action: "default" }, rules[name]) as Rule;
              const custom = name in rules;
              return (
                <tr key={name}>
                  <td className="font-medium">{categoryLabel(name)}</td>
                  <td>
                    <Select aria-label={`${categoryLabel(name)} davranışı`} className="h-8 w-44" disabled={!isAdmin} value={r.action}
                      onChange={(e) => setRule(name, { action: e.target.value as Rule["action"] })}>
                      {Object.entries(ACTION_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                    </Select>
                  </td>
                  <td>
                    <Input aria-label="İnceleme eşiği" type="number" step="0.05" min="0.05" max="0.99" className="h-8 w-24"
                      disabled={!isAdmin || r.action === "allow"} placeholder={String(platform.data?.threshold_review ?? "")}
                      value={r.review ?? ""} onChange={(e) => setRule(name, { review: num(e.target.value) })} />
                  </td>
                  <td>
                    <Input aria-label="Engelleme eşiği" type="number" step="0.05" min="0.05" max="1" className="h-8 w-24"
                      disabled={!isAdmin || r.action !== "default"} placeholder={String(platform.data?.threshold_block ?? "")}
                      value={r.block ?? ""} onChange={(e) => setRule(name, { block: num(e.target.value) })} />
                  </td>
                  <td>{custom && <Tag tone="info">Özel</Tag>}</td>
                </tr>
              );
            })}
          </tbody>
        </Table>
        <p className="border-t border-line px-5 py-3 text-xs text-muted">
          Örnek: çocuklara yönelik bir projede "Müstehcen" ve "Taciz" için "Sıkı: engelle"; bir flört uygulamasında "Müstehcen" için "Yok say".
          Görsel engel listesi eşleşmeleri her projede her zaman engellenir.
        </p>
      </Panel>
    </div>
  );
}

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { api, ApiError, patch } from "../api";
import { useAuth } from "../auth";
import { useToast } from "../components/toast";
import { Button, Field, Input, Mono, Panel, Segmented, Stat, Table, Tag, Textarea } from "../components/ui";
import { can, dateTime, num, relative } from "../format";
import type { PlatformSettings } from "../types";

type Events = {
  events: { id: number; ip: string; rule: string; count: number; action: string; minutes: number | null; created_at: string }[];
  stats: { banned_24h: number; monitored_24h: number; active_auto: number };
};
const RULE: Record<string, string> = {
  auth_fail: "Geçersiz anahtar denemesi", scan: "Tarama (olmayan adresler)", flood: "Olağandışı istek hacmi", panel_login: "Hatalı panel girişi",
};
const ACTION: Record<string, [string, "bad" | "warn" | "neutral"]> = {
  banned: ["Banlandı", "bad"], monitored: ["İzlendi", "warn"], skipped_allowlist: ["İzin listesinde", "neutral"],
};
const MODE_HELP = {
  off: "Otomatik koruma kapalı. Elle eklenen IP engelleri çalışmaya devam eder.",
  monitor: "Kurallar çalışır ve olaylar kaydedilir ama kimse banlanmaz. Eşikleri denemek için.",
  enforce: "Eşiği aşan IP otomatik banlanır. Tekrar eden saldırganda süre uzar: 15 dk → 1 sa → 1 gün → 7 gün.",
};

function duration(m: number | null) {
  if (m == null) return "—";
  return m >= 1440 ? `${m / 1440} gün` : m >= 60 ? `${m / 60} sa` : `${m} dk`;
}

export function AutoBanPanel({ s }: { s: PlatformSettings }) {
  const { user } = useAuth();
  const isAdmin = can(user?.role, "admin");
  const toast = useToast();
  const qc = useQueryClient();
  const events = useQuery({ queryKey: ["autoban-events"], queryFn: () => api<Events>("/security/autoban-events"), refetchInterval: 30_000, enabled: isAdmin });
  const [mode, setMode] = useState(s.autoban_mode);
  const [f, setF] = useState({ auth: "", scan: "", flood: "", panel: "", allow: "" });
  useEffect(() => {
    setMode(s.autoban_mode);
    setF({ auth: String(s.autoban_auth_fail_limit), scan: String(s.autoban_scan_limit), flood: String(s.autoban_flood_limit),
      panel: String(s.autoban_panel_login_limit), allow: (s.autoban_allowlist ?? []).join("\n") });
  }, [s]);

  const save = useMutation({
    mutationFn: (body: object) => patch("/settings", body),
    onSuccess: () => { toast("success", "IP koruması ayarları kaydedildi."); qc.invalidateQueries({ queryKey: ["settings"] }); },
    onError: (e) => toast("error", (e as ApiError).message),
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    save.mutate({
      autoban_mode: mode, autoban_auth_fail_limit: Number(f.auth), autoban_scan_limit: Number(f.scan),
      autoban_flood_limit: Number(f.flood), autoban_panel_login_limit: Number(f.panel),
      autoban_allowlist: f.allow.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean),
    });
  };
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF((p) => ({ ...p, [k]: e.target.value }));

  return (
    <Panel title="Akıllı IP koruması" className="lg:col-span-2" description={MODE_HELP[mode]}
      actions={<Segmented label="Koruma modu" value={mode} onChange={setMode}
        options={[{ value: "off", label: "Kapalı" }, { value: "monitor", label: "Sadece izle" }, { value: "enforce", label: "Otomatik banla" }]} />}>
      <form onSubmit={submit} className="space-y-5">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Field label="Geçersiz anahtar (5 dk)" hint="Anahtar tahmini yapan istemci">
            {(id) => <Input id={id} type="number" min={5} disabled={!isAdmin || mode === "off"} value={f.auth} onChange={set("auth")} />}
          </Field>
          <Field label="Tarama, 404 (5 dk)" hint="/wp-admin, /.env gibi adres arayan bot">
            {(id) => <Input id={id} type="number" min={5} disabled={!isAdmin || mode === "off"} value={f.scan} onChange={set("scan")} />}
          </Field>
          <Field label="İstek hacmi (1 dk)" hint="Tek IP'den gelen tüm istekler">
            {(id) => <Input id={id} type="number" min={100} disabled={!isAdmin || mode === "off"} value={f.flood} onChange={set("flood")} />}
          </Field>
          <Field label="Hatalı panel girişi (15 dk)" hint="Yönetici hesabına kaba kuvvet">
            {(id) => <Input id={id} type="number" min={5} disabled={!isAdmin || mode === "off"} value={f.panel} onChange={set("panel")} />}
          </Field>
        </div>
        <Field label="İzin listesi (asla banlanmaz)" hint="Kendi sunucularınızın IP'leri. Her satıra bir IP veya CIDR (ör. 203.0.113.10 veya 203.0.113.0/24). İç ağ adresleri zaten muaf.">
          {(id) => <Textarea id={id} rows={3} className="font-mono text-xs" disabled={!isAdmin} value={f.allow} onChange={set("allow")} placeholder="203.0.113.10" />}
        </Field>
        {isAdmin && <Button type="submit" variant="primary" loading={save.isPending}>Kaydet</Button>}
      </form>

      {isAdmin && events.data && (
        <div className="mt-6 border-t border-line pt-5">
          <div className="mb-4 grid grid-cols-3 gap-3">
            <Stat label="Son 24 saatte banlanan" value={num(events.data.stats.banned_24h)} tone={events.data.stats.banned_24h ? "block" : undefined} />
            <Stat label="Son 24 saatte izlenen" value={num(events.data.stats.monitored_24h)} />
            <Stat label="Etkin otomatik ban" value={<Link to="/ip-rules" className="hover:underline">{num(events.data.stats.active_auto)}</Link>} />
          </div>
          {events.data.events.length === 0 ? <p className="text-sm text-muted">Henüz olay yok.</p> : (
            <div className="-mx-5">
              <Table>
                <thead><tr><th>IP</th><th>Kural</th><th>Sayı</th><th>İşlem</th><th>Süre</th><th>Zaman</th></tr></thead>
                <tbody>{events.data.events.slice(0, 20).map((e) => {
                  const [label, tone] = ACTION[e.action] ?? [e.action, "neutral"];
                  return (
                    <tr key={e.id}><td><Mono>{e.ip}</Mono></td><td>{RULE[e.rule] ?? e.rule}</td><td>{num(e.count)}</td>
                      <td><Tag tone={tone}>{label}</Tag></td><td>{duration(e.minutes)}</td>
                      <td className="text-muted" title={dateTime(e.created_at)}>{relative(e.created_at)}</td></tr>
                  );
                })}</tbody>
              </Table>
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}

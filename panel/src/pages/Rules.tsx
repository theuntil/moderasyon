import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { api, ApiError, del, patch, post } from "../api";
import { useAuth } from "../auth";
import { useToast } from "../components/toast";
import {
  Button, Empty, ErrorBox, Field, Input, PageHeader, Panel, Segmented, Select, Spinner, Switch, Table, Tag, Textarea,
} from "../components/ui";
import { can, relative } from "../format";
import type { Project } from "../types";

type TermRow = { id: string; project_id: string | null; project_name: string | null; term: string; match_mode: "word" | "contains";
  label: string; severity: "block" | "critical"; created_at: string; created_by: string | null };
type VisualRow = { id: string; label: string; description: string; severity: "block" | "critical"; enabled: boolean;
  created_at: string; created_by: string | null };

export function SeverityTag({ severity }: { severity: string }) {
  return severity === "critical" ? <Tag tone="bad">Kritik</Tag> : <Tag>Engelle</Tag>;
}

function Terms() {
  const { user } = useAuth();
  const isAdmin = can(user?.role, "admin");
  const qc = useQueryClient();
  const toast = useToast();
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/projects") });
  const q = useQuery({ queryKey: ["terms"], queryFn: () => api<TermRow[]>("/rules/terms") });
  const [f, setF] = useState({ term: "", label: "", match_mode: "word", severity: "block", project_id: "" });
  const add = useMutation({
    mutationFn: () => post("/rules/terms", { ...f, project_id: f.project_id || null }),
    onSuccess: () => { toast("success", "Kelime eklendi. 15 saniye içinde tüm isteklerde geçerli olur."); setF({ ...f, term: "" }); qc.invalidateQueries({ queryKey: ["terms"] }); },
    onError: (e) => toast("error", (e as ApiError).message),
  });
  const remove = useMutation({
    mutationFn: (id: string) => del(`/rules/terms/${id}`),
    onSuccess: () => { toast("success", "Kelime kaldırıldı."); qc.invalidateQueries({ queryKey: ["terms"] }); },
  });
  const submit = (e: FormEvent) => { e.preventDefault(); add.mutate(); };

  return (
    <div className="space-y-6">
      {isAdmin && (
        <Panel title="Kelime ekle" description="Metinde ve görsel/videodaki yazıda aranır. 'P.K.K', 'p k k', 'PKK'lı' gibi gizleme denemeleri de yakalanır.">
          <form onSubmit={submit} className="grid gap-4 md:grid-cols-2 lg:grid-cols-6">
            <div className="lg:col-span-2"><Field label="Kelime veya ifade">{(id) => <Input id={id} required maxLength={100} value={f.term} onChange={(e) => setF({ ...f, term: e.target.value })} placeholder="ör. pkk" />}</Field></div>
            <Field label="Etiket" hint="Uygulamana bu etiket döner">{(id) => <Input id={id} required maxLength={48} value={f.label} onChange={(e) => setF({ ...f, label: e.target.value })} placeholder="ör. terör:pkk" />}</Field>
            <Field label="Eşleşme">{(id) => (
              <Select id={id} value={f.match_mode} onChange={(e) => setF({ ...f, match_mode: e.target.value })}>
                <option value="word">Tam kelime</option><option value="contains">Kelime içinde de</option>
              </Select>)}</Field>
            <Field label="Önem">{(id) => (
              <Select id={id} value={f.severity} onChange={(e) => setF({ ...f, severity: e.target.value })}>
                <option value="block">Engelle</option><option value="critical">Kritik (yasal saklama)</option>
              </Select>)}</Field>
            <Field label="Kapsam">{(id) => (
              <Select id={id} value={f.project_id} onChange={(e) => setF({ ...f, project_id: e.target.value })}>
                <option value="">Tüm projeler</option>
                {projects.data?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </Select>)}</Field>
            <div className="lg:col-span-6 flex items-center gap-3">
              <Button type="submit" variant="primary" loading={add.isPending}>Ekle</Button>
              <span className="text-xs text-muted">"Kelime içinde de": kısa kelimelerde yanlış eşleşmeye dikkat (ör. "apo" → "apollo"). Kısa kelimeler için "Tam kelime" seçin.</span>
            </div>
          </form>
        </Panel>
      )}
      <Panel title="Yasaklı kelimeler" padded={false}>
        {q.isLoading ? <Spinner /> : q.error ? <ErrorBox error={q.error} /> : q.data!.length === 0 ? <Empty title="Liste boş">Eklediğiniz kelimeler burada görünür.</Empty> : (
          <Table>
            <thead><tr><th>Kelime</th><th>Etiket</th><th>Eşleşme</th><th>Önem</th><th>Kapsam</th><th>Ekleyen</th><th /></tr></thead>
            <tbody>{q.data!.map((t) => (
              <tr key={t.id}>
                <td className="font-medium">{t.term}</td><td><Tag tone="info">{t.label}</Tag></td>
                <td className="text-muted">{t.match_mode === "word" ? "Tam kelime" : "İçinde de"}</td>
                <td><SeverityTag severity={t.severity} /></td>
                <td>{t.project_name ?? <span className="text-muted">Tüm projeler</span>}</td>
                <td className="text-muted">{t.created_by ?? "—"} · {relative(t.created_at)}</td>
                <td className="text-right">{isAdmin && <Button size="sm" variant="ghostDanger" onClick={() => remove.mutate(t.id)}>Kaldır</Button>}</td>
              </tr>))}
            </tbody>
          </Table>
        )}
      </Panel>
    </div>
  );
}

function Visual() {
  const { user } = useAuth();
  const isAdmin = can(user?.role, "admin");
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery({ queryKey: ["visual-rules"], queryFn: () => api<VisualRow[]>("/rules/visual") });
  const [f, setF] = useState({ label: "", description: "", severity: "critical" });
  const refresh = () => qc.invalidateQueries({ queryKey: ["visual-rules"] });
  const add = useMutation({
    mutationFn: () => post("/rules/visual", f),
    onSuccess: () => { toast("success", "Kural eklendi."); setF({ label: "", description: "", severity: "critical" }); refresh(); },
    onError: (e) => toast("error", (e as ApiError).message),
  });
  const toggle = useMutation({ mutationFn: (r: VisualRow) => patch(`/rules/visual/${r.id}`, { enabled: !r.enabled }), onSuccess: refresh });
  const remove = useMutation({ mutationFn: (id: string) => del(`/rules/visual/${id}`), onSuccess: () => { toast("success", "Kural silindi."); refresh(); } });

  return (
    <div className="space-y-6">
      <Panel title="Nasıl çalışır?">
        <ul className="list-disc space-y-1 pl-5 text-sm text-muted">
          <li>Her görsel ve videonun kareleri AI görsel kontrolüne gönderilir; tarif ettiğiniz sembol, bayrak, amblem, logo veya yazı görünüyorsa içerik engellenir ve etiket uygulamanıza döner.</li>
          <li>AI ve maliyet sayfasında API anahtarı tanımlı olmalı; kontrol ucuz sohbet modeliyle (varsayılan gpt-5-nano) yapılır ve bütçeden düşer.</li>
          <li>Kişiler yüzünden tanınmaz (yasal ve teknik nedenlerle). Kişiye ait görseller isim, yazı ve eşlik eden sembollerle yakalanır; isimleri "Yasaklı kelimeler"e de ekleyin.</li>
          <li>Tarifi İngilizce ve somut yazın (renk, şekil, yazı). Ör: "Red flag with a yellow star inside a green circle".</li>
        </ul>
      </Panel>
      {isAdmin && (
        <Panel title="Kural ekle">
          <form onSubmit={(e) => { e.preventDefault(); add.mutate(); }} className="grid gap-4 md:grid-cols-3">
            <Field label="Etiket" hint="Uygulamana dönen etiket">{(id) => <Input id={id} required maxLength={48} value={f.label} onChange={(e) => setF({ ...f, label: e.target.value })} placeholder="ör. terör:örgüt_bayrağı" />}</Field>
            <Field label="Önem">{(id) => (
              <Select id={id} value={f.severity} onChange={(e) => setF({ ...f, severity: e.target.value })}>
                <option value="critical">Kritik (yasal saklama)</option><option value="block">Engelle</option>
              </Select>)}</Field>
            <div className="md:col-span-3"><Field label="Tarif (modelin arayacağı görüntü)">{(id) => <Textarea id={id} required minLength={10} maxLength={600} rows={2} value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} placeholder="Flag or emblem of ..., typically ..." />}</Field></div>
            <div><Button type="submit" variant="primary" loading={add.isPending}>Ekle</Button></div>
          </form>
        </Panel>
      )}
      <Panel title="Yasaklı görseller ve semboller" padded={false}>
        {q.isLoading ? <Spinner /> : q.error ? <ErrorBox error={q.error} /> : q.data!.length === 0 ? <Empty title="Kural yok" /> : (
          <Table>
            <thead><tr><th>Açık</th><th>Etiket</th><th>Tarif</th><th>Önem</th><th /></tr></thead>
            <tbody>{q.data!.map((r) => (
              <tr key={r.id}>
                <td><Switch label={`${r.label} kuralı`} checked={r.enabled} disabled={!isAdmin} onChange={() => toggle.mutate(r)} /></td>
                <td><Tag tone="info">{r.label}</Tag></td>
                <td className="max-w-[520px] text-sm text-muted">{r.description}</td>
                <td><SeverityTag severity={r.severity} /></td>
                <td className="text-right">{isAdmin && <Button size="sm" variant="ghostDanger" onClick={() => remove.mutate(r.id)}>Sil</Button>}</td>
              </tr>))}
            </tbody>
          </Table>
        )}
      </Panel>
    </div>
  );
}

export function RulesPage() {
  const [tab, setTab] = useState<"terms" | "visual">("terms");
  return (
    <>
      <PageHeader title="Kurallar" description="Engellemek istediğiniz kelimeleri ve görselleri tanımlayın. Her kural bir etiket taşır; etiket uygulamanıza döner ve panelde görünür."
        actions={<Segmented label="Kural türü" value={tab} onChange={setTab} options={[{ value: "terms", label: "Yasaklı kelimeler" }, { value: "visual", label: "Yasaklı görseller" }]} />} />
      {tab === "terms" ? <Terms /> : <Visual />}
    </>
  );
}

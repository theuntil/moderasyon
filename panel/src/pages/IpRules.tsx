import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { SecurityBlockIcon } from "@hugeicons/core-free-icons";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { api, ApiError, del, post } from "../api";
import { Icon } from "../components/icon";
import { useToast } from "../components/toast";
import {
  Button, Empty, ErrorBox, Field, Input, Modal, Mono, PageHeader, Panel, Segmented, Select, Spinner, Table, Tag,
} from "../components/ui";
import { dateTime, num, relative } from "../format";
import type { IpRule, Project } from "../types";

const DURATIONS = [
  { value: "", label: "Süresiz" },
  { value: "1", label: "1 saat" },
  { value: "24", label: "24 saat" },
  { value: "168", label: "7 gün" },
  { value: "720", label: "30 gün" },
];

function AddRuleModal({ open, onClose, projectId }: { open: boolean; onClose: () => void; projectId?: string }) {
  const qc = useQueryClient();
  const toast = useToast();
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/projects"), enabled: open && !projectId });
  const [cidr, setCidr] = useState("");
  const [scope, setScope] = useState(projectId ?? "");
  const [reason, setReason] = useState("");
  const [hours, setHours] = useState("");
  const [error, setError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () => post("/ip-rules", {
      cidr, project_id: (projectId ?? scope) || null, reason: reason || null,
      expires_in_hours: hours ? Number(hours) : null,
    }),
    onSuccess: () => {
      toast("success", `${cidr} engellendi.`);
      qc.invalidateQueries({ queryKey: ["ip-rules"] });
      setCidr(""); setReason(""); setHours(""); setError(null);
      onClose();
    },
    onError: (e) => setError((e as ApiError).message),
  });

  const submit = (e: FormEvent) => { e.preventDefault(); create.mutate(); };

  return (
    <Modal open={open} onClose={onClose} title="IP engelle"
      description="Bu adresten gelen moderasyon istekleri 403 hatasıyla reddedilir."
      footer={<><Button onClick={onClose}>Vazgeç</Button><Button variant="danger" type="submit" form="add-ip" loading={create.isPending}>Engelle</Button></>}>
      <form id="add-ip" onSubmit={submit} className="space-y-4">
        {error && <div role="alert" className="rounded-md bg-block-soft px-3 py-2 text-sm text-block-ink">{error}</div>}
        <Field label="IP adresi veya blok" hint="Tek adres (203.0.113.7) ya da CIDR bloğu (203.0.113.0/24). IPv6 da desteklenir.">
          {(id) => <Input id={id} required className="font-mono" value={cidr} onChange={(e) => setCidr(e.target.value)} placeholder="203.0.113.7" />}
        </Field>
        {!projectId && (
          <Field label="Kapsam">
            {(id) => (
              <Select id={id} value={scope} onChange={(e) => setScope(e.target.value)}>
                <option value="">Tüm platform</option>
                {projects.data?.map((p) => <option key={p.id} value={p.id}>Sadece {p.name}</option>)}
              </Select>
            )}
          </Field>
        )}
        <div className="grid grid-cols-2 gap-3">
          <Field label="Süre">
            {(id) => (
              <Select id={id} value={hours} onChange={(e) => setHours(e.target.value)}>
                {DURATIONS.map((d) => <option key={d.value} value={d.value}>{d.label}</option>)}
              </Select>
            )}
          </Field>
          <Field label="Sebep">
            {(id) => <Input id={id} maxLength={300} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="İsteğe bağlı" />}
          </Field>
        </div>
      </form>
    </Modal>
  );
}

export function IpRulesManager({ projectId }: { projectId?: string }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [adding, setAdding] = useState(false);
  const [scope, setScope] = useState<"all" | "global" | "project">("all");
  const [removing, setRemoving] = useState<IpRule | null>(null);

  const params = projectId ? `project_id=${projectId}` : `scope=${scope}`;
  const q = useQuery({ queryKey: ["ip-rules", params], queryFn: () => api<IpRule[]>(`/ip-rules?${params}`) });

  const remove = useMutation({
    mutationFn: (rule: IpRule) => del(`/ip-rules/${rule.id}`),
    onSuccess: (_, rule) => { toast("success", `${rule.cidr} engeli kaldırıldı.`); qc.invalidateQueries({ queryKey: ["ip-rules"] }); setRemoving(null); },
    onError: (e) => toast("error", (e as Error).message),
  });

  return (
    <>
      <Panel
        padded={false}
        title={projectId ? "Bu projeye özel IP engelleri" : undefined}
        description={projectId ? "Platform genelindeki engeller de bu proje için geçerlidir." : undefined}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            {!projectId && (
              <Segmented label="Kapsam" value={scope} onChange={setScope}
                options={[{ value: "all", label: "Tümü" }, { value: "global", label: "Platform" }, { value: "project", label: "Proje" }]} />
            )}
            <Button variant="primary" size="sm" icon={<Icon icon={SecurityBlockIcon} size={14} />} onClick={() => setAdding(true)}>IP engelle</Button>
          </div>
        }
      >
        {!projectId && <div className="border-b border-line" />}
        {q.isLoading ? <Spinner /> : q.error ? <div className="p-5"><ErrorBox error={q.error} /></div> : q.data!.length === 0 ? (
          <Empty title="Engellenmiş IP yok">Kötüye kullanım gördüğünüz bir adresi buradan engelleyebilirsiniz.</Empty>
        ) : (
          <Table>
            <thead><tr><th>Adres</th><th>Kapsam</th><th>Sebep</th><th>Son 7 gün</th><th>Bitiş</th><th>Ekleyen</th><th /></tr></thead>
            <tbody>
              {q.data!.map((r) => (
                <tr key={r.id} className={r.expired ? "opacity-55" : ""}>
                  <td><Mono>{r.cidr}</Mono>{r.source === "auto" && <span className="ml-2"><Tag tone="warn">Otomatik</Tag></span>}</td>
                  <td>{r.project_id ? (projectId ? <Tag>Bu proje</Tag> : <Link to={`/projects/${r.project_id}`} className="hover:underline">{r.project_name}</Link>) : <Tag tone="info">Tüm platform</Tag>}</td>
                  <td className="max-w-[240px] truncate text-muted">{r.reason || "—"}</td>
                  <td>{num(r.requests_7d)} istek</td>
                  <td>{r.expired ? <Tag>Süresi doldu</Tag> : r.expires_at ? <span title={dateTime(r.expires_at)}>{relative(r.expires_at)}</span> : "Süresiz"}</td>
                  <td className="text-muted">{r.created_by_email ?? "—"}<div className="text-xs">{relative(r.created_at)}</div></td>
                  <td className="text-right"><Button size="sm" variant="ghost" onClick={() => setRemoving(r)}>Kaldır</Button></td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Panel>
      <AddRuleModal open={adding} onClose={() => setAdding(false)} projectId={projectId} />
      <Modal open={!!removing} onClose={() => setRemoving(null)} title="Engeli kaldır"
        description={removing ? `${removing.cidr} adresinden gelen istekler tekrar kabul edilecek.` : ""}
        footer={<><Button onClick={() => setRemoving(null)}>Vazgeç</Button><Button variant="primary" loading={remove.isPending} onClick={() => removing && remove.mutate(removing)}>Engeli kaldır</Button></>} />
    </>
  );
}

export function IpRulesPage() {
  return (
    <>
      <PageHeader title="IP engelleri" description="Belirli adreslerden gelen API isteklerini platform genelinde veya tek bir proje için engelleyin." />
      <IpRulesManager />
    </>
  );
}

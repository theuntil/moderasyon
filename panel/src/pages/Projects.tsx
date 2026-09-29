import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Add01Icon } from "@hugeicons/core-free-icons";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api, ApiError, post } from "../api";
import { useAuth } from "../auth";
import { Icon } from "../components/icon";
import {
  Button, Empty, ErrorBox, Field, Input, Modal, PageHeader, Panel, SecretReveal, Select, Spinner, Tag, Textarea,
} from "../components/ui";
import { VerdictStrip } from "../components/VerdictStrip";
import { can, ms, num, relative } from "../format";
import type { Project } from "../types";

export function slugify(name: string) {
  return name
    .toLocaleLowerCase("tr-TR")
    .replace(/ç/g, "c").replace(/ğ/g, "g").replace(/ı/g, "i").replace(/ö/g, "o").replace(/ş/g, "s").replace(/ü/g, "u")
    .normalize("NFKD").replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 63);
}

function CreateProjectModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [description, setDescription] = useState("");
  const [keyName, setKeyName] = useState("Development");
  const [env, setEnv] = useState<"test" | "live">("test");
  const [result, setResult] = useState<{ id: string; api_key: string } | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  const create = useMutation({
    mutationFn: () => post<{ id: string; api_key: string }>("/projects", {
      name, slug, description: description || null, key_name: keyName, key_environment: env,
    }),
    onSuccess: (r) => { setResult(r); qc.invalidateQueries({ queryKey: ["projects"] }); },
    onError: (e) => setError(e as ApiError),
  });

  const close = () => {
    const created = result;
    setName(""); setSlug(""); setSlugTouched(false); setDescription(""); setKeyName("Development"); setEnv("test");
    setResult(null); setError(null);
    onClose();
    if (created) navigate(`/projects/${created.id}`);
  };

  const submit = (e: FormEvent) => { e.preventDefault(); setError(null); create.mutate(); };

  if (result) {
    return (
      <Modal open={open} onClose={close} title="Proje oluşturuldu" description="Uygulamanız bu anahtarla moderasyon API'sine istek gönderecek."
        footer={<Button variant="primary" onClick={close}>Anahtarı kaydettim, projeye git</Button>}>
        <SecretReveal value={result.api_key} warning="Bu anahtar sadece şimdi gösteriliyor. Kapattıktan sonra tekrar göremezsiniz; kaybederseniz yenisini oluşturmanız gerekir." />
      </Modal>
    );
  }

  return (
    <Modal open={open} onClose={close} title="Yeni proje" description="Her uygulama ayrı bir proje olarak bağlanır; verileri ve anahtarları birbirinden ayrı tutulur."
      footer={<><Button onClick={close}>Vazgeç</Button><Button variant="primary" type="submit" form="create-project" loading={create.isPending}>Projeyi oluştur</Button></>}>
      <form id="create-project" onSubmit={submit} className="space-y-4">
        {error && <div role="alert" className="rounded-md bg-block-soft px-3 py-2 text-sm text-block-ink">{error.message}</div>}
        <Field label="Proje adı">
          {(id) => <Input id={id} required minLength={2} maxLength={100} value={name} placeholder="Örn. KAYS"
            onChange={(e) => { setName(e.target.value); if (!slugTouched) setSlug(slugify(e.target.value)); }} />}
        </Field>
        <Field label="Slug" hint="Küçük harf, rakam ve tire. Daha sonra değiştirilemez." error={error?.field === "slug" ? error.message : undefined}>
          {(id) => <Input id={id} required pattern="[a-z0-9][a-z0-9\-]{1,62}" value={slug} className="font-mono"
            onChange={(e) => { setSlug(e.target.value); setSlugTouched(true); }} />}
        </Field>
        <Field label="Açıklama">
          {(id) => <Textarea id={id} rows={2} maxLength={500} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="İsteğe bağlı" />}
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="İlk anahtarın adı">
            {(id) => <Input id={id} required value={keyName} onChange={(e) => setKeyName(e.target.value)} />}
          </Field>
          <Field label="Ortam">
            {(id) => (
              <Select id={id} value={env} onChange={(e) => setEnv(e.target.value as "test" | "live")}>
                <option value="test">Test (mk_test_)</option>
                <option value="live">Canlı (mk_live_)</option>
              </Select>
            )}
          </Field>
        </div>
      </form>
    </Modal>
  );
}

export function ProjectsPage() {
  const { user } = useAuth();
  const [creating, setCreating] = useState(false);
  const q = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/projects") });
  const isAdmin = can(user?.role, "admin");

  return (
    <>
      <PageHeader
        title="Projeler"
        description="Moderasyon API'sini kullanan uygulamalar. İstatistikler son 30 günü kapsar."
        actions={isAdmin && <Button variant="primary" icon={<Icon icon={Add01Icon} size={16} />} onClick={() => setCreating(true)}>Yeni proje</Button>}
      />
      {q.isLoading ? <Spinner /> : q.error ? <ErrorBox error={q.error} onRetry={() => q.refetch()} /> : q.data!.length === 0 ? (
        <Panel>
          <Empty title="Henüz proje yok" action={isAdmin && <Button variant="primary" onClick={() => setCreating(true)}>İlk projeyi oluştur</Button>}>
            Bir proje oluşturduğunuzda uygulamanız için bir API anahtarı üretilir.
          </Empty>
        </Panel>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {q.data!.map((p) => {
            const allow = Math.max(0, p.requests_30d - p.block_30d - p.review_30d);
            return (
              <Link key={p.id} to={`/projects/${p.id}`}
                className="group rounded-lg border border-line bg-surface p-5 transition-colors hover:border-line-strong">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-semibold group-hover:underline">{p.name}</div>
                    <div className="font-mono text-xs text-muted">{p.slug}</div>
                  </div>
                  {p.status === "active" ? <Tag tone="good">Aktif</Tag> : <Tag tone="bad">Durduruldu</Tag>}
                </div>
                <div className="mt-5 flex items-baseline justify-between">
                  <span className="text-2xl font-semibold tracking-tight">{num(p.requests_30d)}</span>
                  <span className="text-sm text-muted">istek</span>
                </div>
                <div className="mt-2"><VerdictStrip counts={{ allow, review: p.review_30d, block: p.block_30d }} /></div>
                <dl className="mt-4 grid grid-cols-3 gap-2 text-xs">
                  <div><dt className="text-muted">Engellenen</dt><dd className="font-medium">{num(p.block_30d)}</dd></div>
                  <div><dt className="text-muted">Bekleyen</dt><dd className={`font-medium ${p.pending_reviews ? "text-review-ink" : ""}`}>{num(p.pending_reviews)}</dd></div>
                  <div><dt className="text-muted">Ort. süre</dt><dd className="font-medium">{ms(p.latency_30d)}</dd></div>
                </dl>
                <div className="mt-4 border-t border-line pt-3 text-xs text-muted">
                  {p.active_keys} aktif anahtar · son istek {relative(p.last_request_at)}
                </div>
              </Link>
            );
          })}
        </div>
      )}
      <CreateProjectModal open={creating} onClose={() => setCreating(false)} />
    </>
  );
}

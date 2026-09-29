import { useInfiniteQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { api } from "../api";
import { Button, Empty, ErrorBox, Input, Mono, PageHeader, Panel, Select, Spinner, Table } from "../components/ui";
import { dateTime, relative } from "../format";
import type { AuditRow } from "../types";

const ACTION_LABEL: Record<string, string> = {
  "auth.login": "Giriş yaptı",
  "auth.login_failed": "Hatalı giriş denemesi",
  "auth.password_changed": "Şifresini değiştirdi",
  "auth.sessions_revoked": "Diğer oturumlarını kapattı",
  "project.created": "Proje oluşturdu",
  "project.updated": "Projeyi güncelledi",
  "project.enabled": "Projeyi başlattı",
  "project.disabled": "Projeyi durdurdu",
  "project.deleted": "Projeyi sildi",
  "api_key.created": "API anahtarı oluşturdu",
  "api_key.rotated": "API anahtarını yeniledi",
  "api_key.disabled": "API anahtarını durdurdu",
  "api_key.enabled": "API anahtarını etkinleştirdi",
  "api_key.revoked": "API anahtarını iptal etti",
  "ip_rule.created": "IP engelledi",
  "ip_rule.deleted": "IP engelini kaldırdı",
  "review.allow": "İncelemede izin verdi",
  "review.block": "İncelemede engelledi",
  "settings.updated": "Ayarları güncelledi",
  "service.enabled": "Hizmeti açtı",
  "service.disabled": "Hizmeti kapattı",
  "policy.updated": "Karar eşiklerini değiştirdi",
  "admin.created": "Yönetici ekledi",
  "admin.updated": "Yöneticiyi güncelledi",
  "admin.password_reset": "Yönetici şifresini sıfırladı",
  "admin.deleted": "Yöneticiyi sildi",
  "admin.bootstrapped": "İlk yöneticiyi oluşturdu (env)",
  "admin.2fa_reset": "Yöneticinin 2FA'sını sıfırladı",
  "ip.auto_banned": "IP'yi otomatik engelledi",
  "admin.email_linked": "Hesaba e-posta bağlandı (env)",
  "ai.alert": "AI uyarısı",
  "auth.mfa_failed": "Hatalı 2FA kodu",
  "auth.2fa_enabled": "2FA'yı açtı",
  "auth.2fa_disabled": "2FA'yı kapattı",
  "auth.recovery_codes_regenerated": "Kurtarma kodlarını yeniledi",
  "blocklist.deleted": "Görsel engelini kaldırdı",
  "webhook.updated": "Webhook ayarlarını değiştirdi",
  "webhook.secret_rotated": "Webhook anahtarını yeniledi",
};

const DANGEROUS = new Set(["project.deleted", "service.disabled", "api_key.revoked", "admin.deleted", "auth.login_failed", "project.disabled", "auth.mfa_failed", "auth.2fa_disabled", "admin.2fa_reset"]);

const FILTERS = [
  { value: "", label: "Tüm işlemler" },
  { value: "auth", label: "Oturum" },
  { value: "project", label: "Projeler" },
  { value: "api_key", label: "API anahtarları" },
  { value: "ip_rule", label: "IP engelleri" },
  { value: "review", label: "İnceleme" },
  { value: "service", label: "Hizmet aç/kapat" },
  { value: "policy", label: "Eşikler" },
  { value: "admin", label: "Yöneticiler" },
];

function formatValue(v: unknown): string {
  if (v === null || v === undefined) return "boş";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function Details({ details }: { details: Record<string, unknown> }) {
  const entries = Object.entries(details ?? {});
  if (!entries.length) return null;
  return (
    <ul className="mt-1 space-y-0.5 text-xs text-muted">
      {entries.map(([k, v]) => {
        const diff = v && typeof v === "object" && "old" in (v as object) && "new" in (v as object) ? (v as { old: unknown; new: unknown }) : null;
        return (
          <li key={k} className="break-all">
            <span className="text-faint">{k}:</span>{" "}
            {diff ? <><s>{formatValue(diff.old)}</s> → <span className="text-ink">{formatValue(diff.new)}</span></> : formatValue(v)}
          </li>
        );
      })}
    </ul>
  );
}

export function AuditPage() {
  const [action, setAction] = useState("");
  const [actorInput, setActorInput] = useState("");
  const [actor, setActor] = useState("");

  const params = new URLSearchParams();
  if (action) params.set("action", action);
  if (actor) params.set("actor", actor);

  const q = useInfiniteQuery({
    queryKey: ["audit", params.toString()],
    queryFn: ({ pageParam }) => {
      const p = new URLSearchParams(params);
      if (pageParam) p.set("before", String(pageParam));
      return api<{ items: AuditRow[]; next_before: number | null }>(`/audit?${p}`);
    },
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_before ?? undefined,
  });
  const items = q.data?.pages.flatMap((p) => p.items) ?? [];
  const submit = (e: FormEvent) => { e.preventDefault(); setActor(actorInput.trim()); };

  return (
    <>
      <PageHeader title="Denetim kaydı" description="Panelde yapılan her değişiklik kim, ne zaman ve hangi IP'den yaptı bilgisiyle saklanır. Kayıtlar silinemez." />
      <Panel padded={false}>
        <div className="flex flex-wrap items-center gap-2 border-b border-line p-3">
          <Select aria-label="İşlem türü" value={action} onChange={(e) => setAction(e.target.value)} className="w-48">
            {FILTERS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
          </Select>
          <form onSubmit={submit} className="flex gap-2">
            <Input aria-label="Kullanıcı adı" value={actorInput} onChange={(e) => setActorInput(e.target.value)} placeholder="Kullanıcı adı" className="w-64" />
            <Button type="submit">Filtrele</Button>
            {actor && <Button variant="ghost" onClick={() => { setActor(""); setActorInput(""); }}>Temizle</Button>}
          </form>
        </div>
        {q.isLoading ? <Spinner /> : q.error ? <div className="p-5"><ErrorBox error={q.error} onRetry={() => q.refetch()} /></div> : items.length === 0 ? (
          <Empty title="Kayıt yok">Seçtiğiniz filtrelere uyan işlem bulunamadı.</Empty>
        ) : (
          <>
            <Table>
              <thead><tr><th>Zaman</th><th>Yönetici</th><th>İşlem</th><th>Proje</th><th>IP</th></tr></thead>
              <tbody>
                {items.map((a) => (
                  <tr key={a.id} className="[&>td]:align-top">
                    <td className="whitespace-nowrap text-muted" title={dateTime(a.created_at)}>{relative(a.created_at)}<div className="text-xs text-faint">{dateTime(a.created_at)}</div></td>
                    <td>{a.actor_email ?? <span className="text-faint">sistem</span>}</td>
                    <td className="max-w-[420px]">
                      <span className={DANGEROUS.has(a.action) ? "font-medium text-block-ink" : "font-medium"}>{ACTION_LABEL[a.action] ?? a.action}</span>
                      <Details details={a.details} />
                    </td>
                    <td>{a.project_id ? (a.project_name ? <Link to={`/projects/${a.project_id}`} className="hover:underline">{a.project_name}</Link> : <span className="text-faint">silinmiş proje</span>) : <span className="text-faint">—</span>}</td>
                    <td>{a.ip ? <Mono>{a.ip}</Mono> : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </Table>
            {q.hasNextPage && (
              <div className="border-t border-line p-3 text-center">
                <Button onClick={() => q.fetchNextPage()} loading={q.isFetchingNextPage}>Daha eski kayıtlar</Button>
              </div>
            )}
          </>
        )}
      </Panel>
    </>
  );
}

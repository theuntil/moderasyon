import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ComputerIcon } from "@hugeicons/core-free-icons";
import { useState, type FormEvent } from "react";

import { api, ApiError, post } from "../api";
import { useAuth } from "../auth";
import { BrandMark } from "../components/Layout";
import { ThemeToggle } from "../theme";
import { TwoFactorPanel } from "./TwoFactor";
import { Icon } from "../components/icon";
import { useToast } from "../components/toast";
import { Button, ErrorBox, Field, Input, Mono, PageHeader, Panel, Spinner, Tag } from "../components/ui";
import { relative, ROLE_HINT, ROLE_LABEL } from "../format";

function PasswordForm({ onDone, submitLabel = "Şifreyi değiştir" }: { onDone: () => void; submitLabel?: string }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    if (next !== repeat) return setError("Yeni şifreler birbiriyle eşleşmiyor.");
    setLoading(true);
    try {
      await post("/auth/change-password", { current_password: current, new_password: next });
      setCurrent(""); setNext(""); setRepeat("");
      onDone();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Şifre değiştirilemedi.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <form onSubmit={submit} className="space-y-4">
      {error && <p role="alert" className="rounded-md bg-block-soft px-3 py-2 text-sm text-block-ink">{error}</p>}
      <Field label="Mevcut şifre">
        {(id) => <Input id={id} type="password" autoComplete="current-password" required value={current} onChange={(e) => setCurrent(e.target.value)} />}
      </Field>
      <Field label="Yeni şifre" hint="En az 12 karakter; büyük harf, küçük harf ve rakam içermeli.">
        {(id) => <Input id={id} type="password" autoComplete="new-password" required minLength={12} value={next} onChange={(e) => setNext(e.target.value)} />}
      </Field>
      <Field label="Yeni şifre (tekrar)">
        {(id) => <Input id={id} type="password" autoComplete="new-password" required value={repeat} onChange={(e) => setRepeat(e.target.value)} />}
      </Field>
      <Button type="submit" variant="primary" loading={loading} className="w-full sm:w-auto">{submitLabel}</Button>
    </form>
  );
}

export function ForcedPasswordChange() {
  const { user, refresh, logout } = useAuth();
  return (
    <main className="relative grid min-h-full place-items-center px-4 py-10">
      <ThemeToggle className="absolute right-4 top-4" />
      <div className="w-full max-w-sm space-y-5 rounded-lg border border-line bg-surface p-6">
        <div>
          <h1 className="flex items-center gap-2 text-lg font-semibold"><BrandMark />Yeni şifre belirleyin</h1>
          <p className="mt-1 text-sm text-muted">
            <strong className="font-medium text-ink">{user?.email ?? user?.username}</strong> hesabının şifresi geçici ya da panelin
            güvenlik kurallarına uymuyor. Devam etmek için yeni bir şifre belirleyin.
          </p>
        </div>
        <PasswordForm onDone={() => refresh()} submitLabel="Şifreyi kaydet ve devam et" />
        <button onClick={logout} className="text-sm text-muted underline-offset-2 hover:underline">Çıkış yap</button>
      </div>
    </main>
  );
}

type Session = { id: string; ip: string | null; user_agent: string | null; created_at: string; last_seen_at: string; current: boolean };

function describeAgent(ua: string | null) {
  if (!ua) return "Bilinmeyen cihaz";
  const browser = /Edg\//.test(ua) ? "Edge" : /Chrome\//.test(ua) ? "Chrome" : /Firefox\//.test(ua) ? "Firefox" : /Safari\//.test(ua) ? "Safari" : "Tarayıcı";
  const os = /Mac OS X/.test(ua) ? "macOS" : /Windows/.test(ua) ? "Windows" : /Android/.test(ua) ? "Android" : /iPhone|iPad/.test(ua) ? "iOS" : /Linux/.test(ua) ? "Linux" : "";
  return os ? `${browser}, ${os}` : browser;
}

export function AccountPage() {
  const { user } = useAuth();
  const toast = useToast();
  const qc = useQueryClient();
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: () => api<Session[]>("/auth/sessions") });
  const revokeOthers = useMutation({
    mutationFn: () => post<{ revoked: number }>("/auth/sessions/revoke-others"),
    onSuccess: (r) => {
      toast("success", r.revoked ? `${r.revoked} oturum kapatıldı.` : "Başka açık oturum yok.");
      qc.invalidateQueries({ queryKey: ["sessions"] });
    },
    onError: (e) => toast("error", (e as Error).message),
  });

  if (!user) return null;
  return (
    <>
      <PageHeader title="Hesabım" description="Şifrenizi ve açık oturumlarınızı yönetin." />
      <div className="grid gap-6 lg:grid-cols-2">
        <Panel title="Profil">
          <dl className="grid grid-cols-[120px_1fr] gap-y-3 text-sm">
            <dt className="text-muted">E-posta</dt><dd>{user.email || user.username}</dd>
            <dt className="text-muted">Ad</dt><dd>{user.name || "—"}</dd>
            <dt className="text-muted">Rol</dt>
            <dd><Tag tone="info">{ROLE_LABEL[user.role]}</Tag><p className="mt-1 text-sm text-muted">{ROLE_HINT[user.role]}</p></dd>
          </dl>
        </Panel>
        <Panel title="Şifre" description="Değiştirdiğinizde diğer cihazlardaki oturumlarınız kapanır.">
          <PasswordForm onDone={() => { toast("success", "Şifreniz değiştirildi."); qc.invalidateQueries({ queryKey: ["sessions"] }); }} />
        </Panel>
        <TwoFactorPanel />
        <Panel
          className="lg:col-span-2"
          title="Açık oturumlar"
          padded={false}
          actions={<Button size="sm" onClick={() => revokeOthers.mutate()} loading={revokeOthers.isPending}>Diğer oturumları kapat</Button>}
        >
          {sessions.isLoading ? <Spinner /> : sessions.error ? <div className="p-5"><ErrorBox error={sessions.error} /></div> : (
            <ul>
              {sessions.data!.map((s) => (
                <li key={s.id} className="flex items-center gap-3 border-t border-line px-5 py-3 first:border-t-0">
                  <Icon icon={ComputerIcon} size={16} className="text-muted" />
                  <div className="min-w-0 flex-1">
                    <div className="text-sm">{describeAgent(s.user_agent)} {s.current && <Tag tone="good">Bu cihaz</Tag>}</div>
                    <div className="text-xs text-muted"><Mono>{s.ip ?? "—"}</Mono> · son etkinlik {relative(s.last_seen_at)}</div>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>
    </>
  );
}

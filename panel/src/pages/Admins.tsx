import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { UserAdd01Icon } from "@hugeicons/core-free-icons";
import { useState, type FormEvent } from "react";

import { api, ApiError, del, patch, post } from "../api";
import { useAuth } from "../auth";
import { Icon } from "../components/icon";
import { useToast } from "../components/toast";
import {
  Button, ErrorBox, Field, Input, Modal, Mono, PageHeader, Panel, SecretReveal, Select, Spinner, Table, Tag,
} from "../components/ui";
import { can, relative, ROLE_HINT, ROLE_LABEL } from "../format";
import type { AdminRow, Role } from "../types";

const ROLES: Role[] = ["owner", "admin", "moderator", "viewer"];

function CreateAdminModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState<Role>("moderator");
  const [password, setPassword] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const create = useMutation({
    mutationFn: () => post<{ temporary_password: string }>("/admins", { email: email.trim(), name: name || null, role }),
    onSuccess: (r) => { setPassword(r.temporary_password); qc.invalidateQueries({ queryKey: ["admins"] }); },
    onError: (e) => setError((e as ApiError).message),
  });
  const close = () => { setEmail(""); setName(""); setRole("moderator"); setPassword(null); setError(null); onClose(); };

  if (password) {
    return (
      <Modal open={open} onClose={close} title="Yönetici eklendi" description={`${email} için geçici şifre oluşturuldu. İlk girişte kendi şifresini belirlemesi istenecek.`}
        footer={<Button variant="primary" onClick={close}>Tamam</Button>}>
        <SecretReveal value={password} warning="Bu şifreyi kişiye güvenli bir kanaldan iletin. Tekrar gösterilmeyecek." />
      </Modal>
    );
  }
  const submit = (e: FormEvent) => { e.preventDefault(); setError(null); create.mutate(); };
  return (
    <Modal open={open} onClose={close} title="Yönetici ekle"
      footer={<><Button onClick={close}>Vazgeç</Button><Button variant="primary" type="submit" form="create-admin" loading={create.isPending}>Ekle</Button></>}>
      <form id="create-admin" onSubmit={submit} className="space-y-4">
        {error && <div role="alert" className="rounded-md bg-block-soft px-3 py-2 text-sm text-block-ink">{error}</div>}
        <Field label="E-posta" hint="Panele bu e-postayla giriş yapılır.">
          {(id) => <Input id={id} type="email" required autoCapitalize="none" spellCheck={false} value={email} onChange={(e) => setEmail(e.target.value)} />}
        </Field>
        <Field label="Ad">{(id) => <Input id={id} maxLength={100} value={name} onChange={(e) => setName(e.target.value)} placeholder="İsteğe bağlı" />}</Field>
        <Field label="Rol" hint={ROLE_HINT[role]}>
          {(id) => <Select id={id} value={role} onChange={(e) => setRole(e.target.value as Role)}>{ROLES.map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}</Select>}
        </Field>
      </form>
    </Modal>
  );
}

type Pending =
  | { kind: "role"; admin: AdminRow; role: Role }
  | { kind: "status"; admin: AdminRow }
  | { kind: "reset"; admin: AdminRow }
  | { kind: "delete"; admin: AdminRow }
  | { kind: "reset2fa"; admin: AdminRow };

export function AdminsPage() {
  const { user } = useAuth();
  const toast = useToast();
  const qc = useQueryClient();
  const isOwner = can(user?.role, "owner");
  const [creating, setCreating] = useState(false);
  const [pending, setPending] = useState<Pending | null>(null);
  const [tempPassword, setTempPassword] = useState<string | null>(null);
  const q = useQuery({ queryKey: ["admins"], queryFn: () => api<AdminRow[]>("/admins") });

  const run = useMutation({
    mutationFn: async (p: Pending) => {
      if (p.kind === "role") return patch(`/admins/${p.admin.id}`, { role: p.role });
      if (p.kind === "status") return patch(`/admins/${p.admin.id}`, { status: p.admin.status === "active" ? "disabled" : "active" });
      if (p.kind === "reset") return post<{ temporary_password: string }>(`/admins/${p.admin.id}/reset-password`);
      if (p.kind === "reset2fa") return post(`/admins/${p.admin.id}/reset-2fa`);
      return del(`/admins/${p.admin.id}`);
    },
    onSuccess: (r, p) => {
      qc.invalidateQueries({ queryKey: ["admins"] });
      setPending(null);
      if (p.kind === "reset") setTempPassword((r as { temporary_password: string }).temporary_password);
      else toast("success", { role: "Rol güncellendi.", status: p.admin.status === "active" ? "Hesap devre dışı bırakıldı." : "Hesap etkinleştirildi.", delete: "Yönetici silindi.", reset2fa: "İki adımlı doğrulama sıfırlandı." }[p.kind]);
    },
    onError: (e) => { toast("error", (e as Error).message); setPending(null); },
  });

  const confirmText = (p: Pending) => {
    switch (p.kind) {
      case "role": return { title: "Rolü değiştir", text: `${p.admin.email ?? p.admin.username} kullanıcısının rolü ${ROLE_LABEL[p.role]} olacak. Açık oturumları kapatılır.`, button: "Rolü değiştir", danger: false };
      case "status": return p.admin.status === "active"
        ? { title: "Hesabı devre dışı bırak", text: `${p.admin.email ?? p.admin.username} panele giriş yapamayacak ve açık oturumları kapanacak.`, button: "Devre dışı bırak", danger: true }
        : { title: "Hesabı etkinleştir", text: `${p.admin.email ?? p.admin.username} tekrar giriş yapabilecek.`, button: "Etkinleştir", danger: false };
      case "reset": return { title: "Şifreyi sıfırla", text: `${p.admin.email ?? p.admin.username} için geçici bir şifre oluşturulacak ve açık oturumları kapanacak.`, button: "Şifreyi sıfırla", danger: false };
      case "reset2fa": return { title: "2FA'yı sıfırla", text: `${p.admin.email ?? p.admin.username} iki adımlı doğrulaması kapatılacak ve oturumları sonlandırılacak. Telefonunu kaybettiyse kullanın; giriş yaptıktan sonra yeniden kurmalı.`, button: "Sıfırla", danger: true };
      case "delete": return { title: "Yöneticiyi sil", text: `${p.admin.email ?? p.admin.username} kalıcı olarak silinecek. Denetim kayıtlarındaki geçmiş işlemleri korunur.`, button: "Sil", danger: true };
    }
  };

  return (
    <>
      <PageHeader title="Yöneticiler" description="Panele erişimi olan kişiler ve rolleri."
        actions={isOwner && <Button variant="primary" icon={<Icon icon={UserAdd01Icon} size={16} />} onClick={() => setCreating(true)}>Yönetici ekle</Button>} />

      <div className="mb-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {ROLES.map((r) => (
          <div key={r} className="rounded-lg border border-line bg-surface px-4 py-3">
            <div className="text-sm font-medium">{ROLE_LABEL[r]}</div>
            <div className="text-xs text-muted">{ROLE_HINT[r]}</div>
          </div>
        ))}
      </div>

      <Panel padded={false}>
        {q.isLoading ? <Spinner /> : q.error ? <div className="p-5"><ErrorBox error={q.error} /></div> : (
          <Table>
            <thead><tr><th>Kişi</th><th>Rol</th><th>Durum</th><th>2FA</th><th>Son giriş</th><th>Oturum</th><th /></tr></thead>
            <tbody>
              {q.data!.map((a) => {
                const self = a.id === user?.id;
                return (
                  <tr key={a.id}>
                    <td>
                      <div className="font-medium">{a.email ?? a.username}{self && <span className="ml-2 text-xs text-muted">(siz)</span>}</div>
                      {a.name && a.name !== a.email && a.name !== a.username && <div className="text-xs text-muted">{a.name}</div>}
                    </td>
                    <td>
                      {isOwner && !self ? (
                        <Select aria-label="Rol" value={a.role} className="h-8 w-36" onChange={(e) => setPending({ kind: "role", admin: a, role: e.target.value as Role })}>
                          {ROLES.map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}
                        </Select>
                      ) : <Tag tone="info">{ROLE_LABEL[a.role]}</Tag>}
                    </td>
                    <td className="space-x-1">
                      {a.status === "active" ? <Tag tone="good">Aktif</Tag> : <Tag tone="bad">Devre dışı</Tag>}
                      {a.locked && <Tag tone="warn">Kilitli</Tag>}
                      {a.must_change_password && <Tag>Şifre bekleniyor</Tag>}
                    </td>
                    <td>{a.totp_enabled ? <Tag tone="good">Açık</Tag> : <Tag tone="warn">Kapalı</Tag>}</td>
                    <td>{relative(a.last_login_at)}{a.last_login_ip && <div><Mono className="text-muted">{a.last_login_ip}</Mono></div>}</td>
                    <td>{a.active_sessions}</td>
                    <td>
                      {isOwner && !self && (
                        <div className="flex justify-end gap-1">
                          <Button size="sm" variant="ghost" onClick={() => setPending({ kind: "reset", admin: a })}>Şifre sıfırla</Button>
                          {a.totp_enabled && <Button size="sm" variant="ghost" onClick={() => setPending({ kind: "reset2fa", admin: a })}>2FA sıfırla</Button>}
                          <Button size="sm" variant="ghost" onClick={() => setPending({ kind: "status", admin: a })}>{a.status === "active" ? "Durdur" : "Etkinleştir"}</Button>
                          <Button size="sm" variant="ghostDanger" onClick={() => setPending({ kind: "delete", admin: a })}>Sil</Button>
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        )}
      </Panel>

      <CreateAdminModal open={creating} onClose={() => setCreating(false)} />
      {pending && (() => {
        const c = confirmText(pending);
        return (
          <Modal open onClose={() => setPending(null)} title={c.title} description={c.text}
            footer={<><Button onClick={() => setPending(null)}>Vazgeç</Button><Button variant={c.danger ? "danger" : "primary"} loading={run.isPending} onClick={() => run.mutate(pending)}>{c.button}</Button></>} />
        );
      })()}
      <Modal open={!!tempPassword} onClose={() => setTempPassword(null)} title="Geçici şifre" footer={<Button variant="primary" onClick={() => setTempPassword(null)}>Tamam</Button>}>
        {tempPassword && <SecretReveal value={tempPassword} warning="İlk girişte yeni şifre belirlemesi istenecek. Bu şifre tekrar gösterilmeyecek." />}
      </Modal>
    </>
  );
}

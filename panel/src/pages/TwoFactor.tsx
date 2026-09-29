import { useQuery, useQueryClient } from "@tanstack/react-query";
import QRCode from "qrcode";
import { useEffect, useState, type FormEvent } from "react";

import { ApiError, post } from "../api";
import { useAuth } from "../auth";
import { useToast } from "../components/toast";
import { Button, CopyButton, Field, Input, Modal, Mono, Panel, Tag } from "../components/ui";

function RecoveryCodes({ codes, onClose }: { codes: string[]; onClose: () => void }) {
  const text = codes.join("\n");
  return (
    <Modal open onClose={onClose} title="Kurtarma kodları"
      description="Telefonunuza erişemezseniz bu kodlarla giriş yapabilirsiniz. Her kod bir kez kullanılır. Güvenli bir yere (ör. şifre yöneticisi) kaydedin; tekrar gösterilmeyecek."
      footer={<><CopyButton value={text} label="Kodları kopyala" /><Button variant="primary" onClick={onClose}>Kaydettim</Button></>}>
      <div className="grid grid-cols-2 gap-2 rounded-md border border-line bg-sunken p-4 font-mono text-sm">
        {codes.map((c) => <span key={c}>{c}</span>)}
      </div>
    </Modal>
  );
}

export function TwoFactorPanel() {
  const { user, refresh } = useAuth();
  const toast = useToast();
  const qc = useQueryClient();
  const enabled = !!user?.totp_enabled;
  const [setup, setSetup] = useState<{ secret: string; otpauth_uri: string } | null>(null);
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [mode, setMode] = useState<"disable" | "regenerate" | null>(null);

  const qr = useQuery({
    queryKey: ["totp-qr", setup?.otpauth_uri],
    queryFn: () => QRCode.toDataURL(setup!.otpauth_uri, { margin: 1, width: 200 }),
    enabled: !!setup,
    staleTime: Infinity,
  });
  useEffect(() => { setCode(""); setError(null); setPassword(""); }, [mode, setup]);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true); setError(null);
    try { await fn(); } catch (e) { setError(e instanceof ApiError ? e.message : "İşlem tamamlanamadı."); } finally { setBusy(false); }
  };
  const done = async () => { await refresh(); qc.invalidateQueries({ queryKey: ["sessions"] }); };

  const startSetup = () => run(async () => setSetup(await post("/auth/2fa/setup")));
  const confirm = (e: FormEvent) => { e.preventDefault(); run(async () => {
    const r = await post<{ recovery_codes: string[] }>("/auth/2fa/enable", { code });
    setSetup(null); setCodes(r.recovery_codes); toast("success", "İki adımlı doğrulama açıldı."); await done();
  }); };
  const disable = (e: FormEvent) => { e.preventDefault(); run(async () => {
    await post("/auth/2fa/disable", { password, code }); setMode(null); toast("success", "İki adımlı doğrulama kapatıldı."); await done();
  }); };
  const regenerate = (e: FormEvent) => { e.preventDefault(); run(async () => {
    const r = await post<{ recovery_codes: string[] }>("/auth/2fa/recovery-codes", { code }); setMode(null); setCodes(r.recovery_codes); await done();
  }); };

  return (
    <Panel className="lg:col-span-2" title="İki adımlı doğrulama"
      description="Şifreniz ele geçse bile, telefonunuzdaki doğrulama uygulaması olmadan hesabınıza girilemez."
      actions={enabled ? <Tag tone="good">Açık</Tag> : <Tag tone="warn">Kapalı</Tag>}>
      {!enabled && !setup && (
        <div className="flex flex-wrap items-center justify-between gap-4">
          <p className="max-w-[60ch] text-sm text-muted">Google Authenticator, Microsoft Authenticator, 1Password veya benzeri bir uygulama gerekir. Panel yöneticileri için şiddetle önerilir.</p>
          <Button variant="primary" onClick={startSetup} loading={busy}>Kurulumu başlat</Button>
        </div>
      )}

      {!enabled && setup && (
        <form onSubmit={confirm} className="grid gap-6 md:grid-cols-[200px_minmax(0,1fr)]">
          <div className="rounded-md border border-line bg-white p-2">{qr.data ? <img src={qr.data} alt="Doğrulama uygulaması için QR kod" className="w-full" /> : <div className="aspect-square" />}</div>
          <div className="space-y-4">
            <ol className="list-decimal space-y-1 pl-5 text-sm text-muted">
              <li>Doğrulama uygulamasında yeni hesap ekleyin ve QR kodu okutun.</li>
              <li>Okutamıyorsanız anahtarı elle girin: <Mono className="break-all text-ink">{setup.secret}</Mono></li>
              <li>Uygulamanın gösterdiği 6 haneli kodu aşağıya yazın.</li>
            </ol>
            <Field label="Doğrulama kodu" error={error ?? undefined}>
              {(id) => <Input id={id} inputMode="numeric" autoComplete="one-time-code" required maxLength={6} className="w-40 font-mono tracking-widest" value={code} onChange={(e) => setCode(e.target.value)} />}
            </Field>
            <div className="flex gap-2">
              <Button type="submit" variant="primary" loading={busy}>Doğrula ve aç</Button>
              <Button type="button" variant="ghost" onClick={() => setSetup(null)}>Vazgeç</Button>
            </div>
          </div>
        </form>
      )}

      {enabled && !mode && (
        <div className="flex flex-wrap items-center justify-between gap-4">
          <p className="text-sm text-muted">Kalan kurtarma kodu: <strong className="text-ink">{user?.recovery_codes_left ?? "—"}</strong>{(user?.recovery_codes_left ?? 10) <= 3 && " · yenilemeniz önerilir"}</p>
          <div className="flex gap-2">
            <Button onClick={() => setMode("regenerate")}>Kurtarma kodlarını yenile</Button>
            <Button variant="ghostDanger" onClick={() => setMode("disable")}>Kapat</Button>
          </div>
        </div>
      )}

      {enabled && mode && (
        <form onSubmit={mode === "disable" ? disable : regenerate} className="max-w-sm space-y-4">
          {mode === "disable" && (
            <Field label="Şifreniz">{(id) => <Input id={id} type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />}</Field>
          )}
          <Field label="Doğrulama uygulamasındaki kod" error={error ?? undefined}>
            {(id) => <Input id={id} inputMode="numeric" autoComplete="one-time-code" required maxLength={6} className="w-40 font-mono tracking-widest" value={code} onChange={(e) => setCode(e.target.value)} />}
          </Field>
          <div className="flex gap-2">
            <Button type="submit" variant={mode === "disable" ? "danger" : "primary"} loading={busy}>{mode === "disable" ? "İki adımlı doğrulamayı kapat" : "Yeni kodlar oluştur"}</Button>
            <Button type="button" variant="ghost" onClick={() => setMode(null)}>Vazgeç</Button>
          </div>
        </form>
      )}

      {codes && <RecoveryCodes codes={codes} onClose={() => setCodes(null)} />}
    </Panel>
  );
}

import { useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { ApiError, post } from "../api";
import { BrandMark } from "../components/Layout";
import { Button, Field, Input } from "../components/ui";
import { ThemeToggle } from "../theme";
import type { Me } from "../types";

export function LoginPage() {
  const qc = useQueryClient();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [mfaToken, setMfaToken] = useState<string | null>(null);
  const [code, setCode] = useState("");

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      if (mfaToken) {
        const me = await post<Me & { recovery_codes_left?: number }>("/auth/mfa", { mfa_token: mfaToken, code: code.trim() });
        qc.setQueryData(["me"], me);
        return;
      }
      const r = await post<Me | { mfa_required: true; mfa_token: string }>("/auth/login", { email: email.trim(), password });
      if ("mfa_required" in r) {
        setMfaToken(r.mfa_token);
        setPassword("");
      } else {
        qc.setQueryData(["me"], r);
      }
    } catch (err) {
      const apiErr = err instanceof ApiError ? err : null;
      setError(apiErr ? apiErr.message : "Sunucuya ulaşılamadı.");
      if (apiErr?.code === "mfa_expired") { setMfaToken(null); setCode(""); }
      else if (mfaToken) setCode("");
      else setPassword("");
    } finally {
      setLoading(false);
    }
  };

  if (mfaToken) {
    return (
      <main className="relative grid min-h-full place-items-center px-4 py-10">
        <ThemeToggle className="absolute right-4 top-4" />
        <form onSubmit={submit} className="w-full max-w-sm space-y-5 rounded-lg border border-line bg-surface p-6">
          <div>
            <h1 className="flex items-center gap-2 text-lg font-semibold"><BrandMark />İki adımlı doğrulama</h1>
            <p className="text-sm text-muted">Doğrulama uygulamanızdaki 6 haneli kodu girin. Telefonunuz yanınızda değilse kurtarma kodlarınızdan birini kullanabilirsiniz.</p>
          </div>
          <Field label="Kod">
            {(id) => (
              <Input id={id} inputMode="numeric" autoComplete="one-time-code" autoFocus required maxLength={16}
                className="text-center font-mono text-lg tracking-[0.3em]" value={code} onChange={(e) => setCode(e.target.value)} />
            )}
          </Field>
          {error && <p role="alert" className="text-sm text-block-ink">{error}</p>}
          <Button type="submit" variant="primary" loading={loading} className="w-full">Doğrula</Button>
          <button type="button" onClick={() => { setMfaToken(null); setCode(""); setError(null); }} className="text-sm text-muted hover:underline">
            Farklı hesapla giriş yap
          </button>
        </form>
      </main>
    );
  }

  return (
    <main className="relative grid min-h-full place-items-center px-4 py-10">
      <ThemeToggle className="absolute right-4 top-4" />
      <form onSubmit={submit} className="w-full max-w-sm space-y-5 rounded-lg border border-line bg-surface p-6">
        <div>
          <h1 className="flex items-center gap-2 text-lg font-semibold"><BrandMark />Moderasyon</h1>
          <p className="text-sm text-muted">Yönetim paneline giriş yapın.</p>
        </div>

        <Field label="E-posta">
          {(id) => (
            <Input id={id} type="email" autoComplete="username" autoCapitalize="none" spellCheck={false} required autoFocus
              value={email} onChange={(e) => setEmail(e.target.value)} />
          )}
        </Field>
        <Field label="Şifre">
          {(id) => (
            <Input id={id} type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
          )}
        </Field>

        {error && <p role="alert" className="text-sm text-block-ink">{error}</p>}
        <Button type="submit" variant="primary" loading={loading} className="w-full">
          {loading ? "Giriş yapılıyor…" : "Giriş yap"}
        </Button>
        <details className="text-xs text-muted">
          <summary className="cursor-pointer select-none hover:text-ink">Giriş yapamıyor musunuz?</summary>
          <ul className="mt-2 list-disc space-y-1 pl-4">
            <li>İlk girişte belirlediğiniz yeni şifre geçerlidir; env'deki şifre sadece hesabı ilk oluştururken kullanılır.</li>
            <li>5 hatalı denemeden sonra hesap 15 dakika kilitlenir.</li>
            <li>Şifreyi sıfırlamak için: env'e <code>ADMIN_RESET_PASSWORD=true</code> ekleyip yeniden deploy edin (env'deki şifre geçerli olur), veya api terminalinde <code>python -m app.cli reset-admin-password --email …</code></li>
          </ul>
        </details>
      </form>
    </main>
  );
}

// Admin API istemcisi. Tüm istekler aynı origin'deki /api yoluna gider (nginx → admin-api).
// Oturum HttpOnly cookie ile taşınır; değiştiren isteklerde CSRF başlığı zorunludur.

export class ApiError extends Error {
  status: number;
  code: string;
  field?: string;
  constructor(status: number, code: string, message: string, field?: string) {
    super(message);
    this.status = status;
    this.code = code;
    this.field = field;
  }
}

const MESSAGES: Record<string, string> = {
  not_authenticated: "Oturumunuzun süresi doldu. Lütfen tekrar giriş yapın.",
  insufficient_role: "Bu işlem için yetkiniz yok.",
  csrf_check_failed: "Güvenlik kontrolü başarısız oldu. Sayfayı yenileyin.",
  password_change_required: "Devam etmeden önce şifrenizi değiştirmeniz gerekiyor.",
  ip_blocked: "Bu IP adresi çok sayıda hatalı deneme nedeniyle geçici olarak engellendi. Bir süre sonra tekrar deneyin ya da sunucuda engeli kaldırın: python -m app.cli unban-ip --ip <IP>",
};

export async function api<T = unknown>(path: string, options: { method?: string; body?: unknown } = {}): Promise<T> {
  const method = options.method ?? "GET";
  const res = await fetch(`/api${path}`, {
    method,
    credentials: "same-origin",
    headers: {
      "X-Requested-With": "moderation-panel",
      ...(options.body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });

  if (res.status === 204) return undefined as T;
  const data = await res.json().catch(() => null);

  if (!res.ok) {
    const detail = data?.detail;
    const code = typeof detail === "object" && detail?.error ? detail.error : `http_${res.status}`;
    const message =
      (code === "ip_blocked" && MESSAGES[code]) ||
      (typeof detail === "object" && detail?.message) ||
      MESSAGES[code] ||
      (res.status >= 500 ? "Sunucuda bir hata oluştu. Birkaç saniye sonra tekrar deneyin." : "İstek tamamlanamadı.");
    const err = new ApiError(res.status, code, message, detail?.field);
    if (res.status === 401 && path !== "/auth/login") window.dispatchEvent(new CustomEvent("auth:expired"));
    if (code === "password_change_required") window.dispatchEvent(new CustomEvent("auth:password-change"));
    throw err;
  }
  return data as T;
}

export const post = <T = unknown>(path: string, body?: unknown) => api<T>(path, { method: "POST", body: body ?? {} });
export const patch = <T = unknown>(path: string, body: unknown) => api<T>(path, { method: "PATCH", body });
export const del = <T = unknown>(path: string, body?: unknown) => api<T>(path, { method: "DELETE", body });

import type { Decision, Role } from "./types";

const nf = new Intl.NumberFormat("tr-TR");
export const num = (n: number | null | undefined) => (n == null ? "—" : nf.format(n));

export const pct = (part: number, total: number) =>
  total > 0 ? `%${new Intl.NumberFormat("tr-TR", { maximumFractionDigits: 1 }).format((part / total) * 100)}` : "—";

export const ms = (v: number | null | undefined) => {
  if (v == null) return "—";
  return v >= 1000 ? `${new Intl.NumberFormat("tr-TR", { maximumFractionDigits: 1 }).format(v / 1000)} sn` : `${v} ms`;
};

const dtf = new Intl.DateTimeFormat("tr-TR", { dateStyle: "medium", timeStyle: "short" });
export const dateTime = (iso: string | null | undefined) => (iso ? dtf.format(new Date(iso)) : "—");

const rtf = new Intl.RelativeTimeFormat("tr-TR", { numeric: "auto" });
export function relative(iso: string | null | undefined): string {
  if (!iso) return "hiç";
  const diff = (new Date(iso).getTime() - Date.now()) / 1000;
  const abs = Math.abs(diff);
  if (abs < 45) return "az önce";
  if (abs < 3600) return rtf.format(Math.round(diff / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), "hour");
  if (abs < 86400 * 30) return rtf.format(Math.round(diff / 86400), "day");
  return dateTime(iso);
}

export const DECISION_LABEL: Record<Decision, string> = { allow: "İzin verildi", review: "İncelemede", block: "Engellendi" };
export const DECISION_SHORT: Record<Decision, string> = { allow: "İzin", review: "İncele", block: "Engelle" };

export const ROLE_LABEL: Record<Role, string> = { owner: "Sahip", admin: "Yönetici", moderator: "Moderatör", viewer: "İzleyici" };
export const ROLE_HINT: Record<Role, string> = {
  owner: "Her şeyi yönetir, yönetici ekler ve çıkarır.",
  admin: "Projeleri, anahtarları, IP kurallarını ve ayarları yönetir.",
  moderator: "İnceleme kuyruğunda karar verir, kararları görür.",
  viewer: "İstatistikleri ve kararları sadece görüntüler.",
};
const RANK: Record<Role, number> = { viewer: 0, moderator: 1, admin: 2, owner: 3 };
export const can = (role: Role | undefined, minimum: Role) => !!role && RANK[role] >= RANK[minimum];

export const CATEGORY_LABEL: Record<string, string> = {
  harassment: "Taciz / hakaret", sexual: "Cinsel içerik (açık)", hate: "Nefret söylemi", violence: "Şiddet",
  threat: "Tehdit", spam: "Spam", scam: "Dolandırıcılık", extremism: "Aşırıcılık", self_harm: "Kendine zarar",
  nudity: "Çıplaklık", suggestive: "Müstehcen", blocklist_match: "Engel listesi eşleşmesi",
  weapons: "Silah", drugs: "Uyuşturucu", illicit: "Yasa dışı faaliyet", sexual_minors: "Reşit olmayan (cinsel)",
};

export const CONTENT_TYPE_LABEL: Record<string, string> = { text: "Metin", image: "Görsel", video: "Video" };

export function duration(msValue: number | null | undefined): string {
  if (msValue == null) return "—";
  const s = Math.round(msValue / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export function bytes(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${new Intl.NumberFormat("tr-TR", { maximumFractionDigits: 1 }).format(n / 1024 / 1024)} MB`;
}
export const categoryLabel = (name: string) => CATEGORY_LABEL[name] ?? name;

export const STATUS_LABEL: Record<string, string> = {
  queued: "Kuyrukta", processing: "İşleniyor", completed: "Tamamlandı", failed: "Hata",
};

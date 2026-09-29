import { Alert02Icon, Cancel01Icon, Copy01Icon, Loading03Icon, Tick02Icon } from "@hugeicons/core-free-icons";
import {
  forwardRef, useEffect, useId, useRef, useState,
  type ButtonHTMLAttributes, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";
import { createPortal } from "react-dom";

import { ApiError } from "../api";
import { DECISION_SHORT } from "../format";
import type { Decision } from "../types";
import { Icon } from "./icon";

// ---------------------------------------------------------------- Button

type Variant = "primary" | "secondary" | "danger" | "ghost" | "ghostDanger" | "allow" | "block";
const VARIANTS: Record<Variant, string> = {
  primary: "bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50",
  secondary: "border border-line bg-transparent text-ink hover:bg-sunken disabled:opacity-50",
  danger: "bg-block text-white hover:bg-block/90 disabled:opacity-50",
  ghost: "text-muted hover:text-ink hover:bg-sunken disabled:opacity-50",
  ghostDanger: "text-block-ink hover:bg-block-soft disabled:opacity-50",
  allow: "bg-allow text-white hover:bg-allow/90 dark:text-black disabled:opacity-50",
  block: "bg-block text-white hover:bg-block/90 disabled:opacity-50",
};

export const Button = forwardRef<
  HTMLButtonElement,
  ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: "sm" | "md"; loading?: boolean; icon?: ReactNode }
>(function Button({ variant = "secondary", size = "md", loading, icon, children, className = "", disabled, ...rest }, ref) {
  const sizing = size === "sm" ? "h-8 px-3 text-xs gap-1.5" : "h-9 px-4 text-sm gap-2";
  return (
    <button
      ref={ref}
      className={`inline-flex shrink-0 items-center justify-center rounded-md font-medium transition-colors disabled:cursor-not-allowed ${sizing} ${VARIANTS[variant]} ${className}`}
      disabled={disabled || loading}
      {...rest}
    >
      {loading ? <Icon icon={Loading03Icon} size={16} className="animate-spin" /> : icon}
      {children}
    </button>
  );
});

// ---------------------------------------------------------------- Layout pieces

export function PageHeader({ title, description, actions }: { title: string; description?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold">{title}</h1>
        {description && <p className="mt-0.5 max-w-[70ch] text-sm text-muted">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

export function Panel({
  title, description, actions, children, className = "", padded = true,
}: { title?: ReactNode; description?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; padded?: boolean }) {
  return (
    <section className={`rounded-lg border border-line bg-surface ${className}`}>
      {(title || actions) && (
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-5 py-3">
          <div>
            {title && <h2 className="text-sm font-semibold">{title}</h2>}
            {description && <p className="text-xs text-muted">{description}</p>}
          </div>
          {actions}
        </div>
      )}
      <div className={padded ? "p-5" : ""}>{children}</div>
    </section>
  );
}

export function Stat({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: ReactNode; tone?: "block" | "review" }) {
  const color = tone === "block" ? "text-block-ink" : tone === "review" ? "text-review-ink" : "";
  return (
    <div>
      <div className="text-xs text-muted">{label}</div>
      <div className={`mt-1 text-xl font-semibold tabular-nums ${color}`}>{value}</div>
      {hint && <div className="mt-0.5 text-[12px] text-faint">{hint}</div>}
    </div>
  );
}

// ---------------------------------------------------------------- Badges

export function DecisionBadge({ decision, human }: { decision: Decision | null; human?: boolean }) {
  if (!decision) return <span className="text-faint">—</span>;
  const styles: Record<Decision, string> = {
    allow: "bg-allow-soft text-allow-ink",
    review: "bg-review-soft text-review-ink",
    block: "bg-block-soft text-block-ink",
  };
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-sm px-1.5 py-0.5 text-xs font-medium ${styles[decision]}`}>
      <span className="size-1.5 rounded-full bg-current" />
      {DECISION_SHORT[decision]}
      {human && <span className="font-normal opacity-80">· insan</span>}
    </span>
  );
}

export function Tag({ children, tone = "neutral" }: { children: ReactNode; tone?: "neutral" | "good" | "warn" | "bad" | "info" }) {
  const t = {
    neutral: "bg-sunken text-muted",
    good: "bg-allow-soft text-allow-ink",
    warn: "bg-review-soft text-review-ink",
    bad: "bg-block-soft text-block-ink",
    info: "bg-primary/12 text-primary",
  }[tone];
  return <span className={`inline-flex items-center rounded-sm px-1.5 py-0.5 text-xs font-medium ${t}`}>{children}</span>;
}

export const Mono = ({ children, className = "" }: { children: ReactNode; className?: string }) => (
  <span className={`font-mono text-xs ${className}`}>{children}</span>
);

// ---------------------------------------------------------------- Forms

export function Field({
  label, hint, error, children,
}: { label: string; hint?: ReactNode; error?: string; children: (id: string) => ReactNode }) {
  const id = useId();
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-medium">{label}</label>
      {children(id)}
      {error ? <p className="text-xs text-block-ink">{error}</p> : hint ? <p className="text-xs text-muted">{hint}</p> : null}
    </div>
  );
}

// Genişlik verilmemişse alan tüm satırı kaplar; className ile w-40 gibi bir genişlik verilebilir
const width = (className: string) => (/(^|\s)w-/.test(className) ? "" : "w-full");

const inputBase =
  "rounded-md border border-line-strong bg-surface px-3 text-sm text-ink placeholder:text-faint focus:border-focus focus:outline-none focus:ring-2 focus:ring-focus/25 disabled:bg-sunken disabled:text-muted";

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function Input({ className = "", ...p }, ref) {
  return <input ref={ref} className={`h-9 ${width(className)} ${inputBase} ${className}`} {...p} />;
});

export function Select({ className = "", children, ...p }: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={`h-9 ${width(className)} ${inputBase} pr-8 ${className}`} {...p}>{children}</select>;
}

export function Textarea({ className = "", ...p }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={`py-2 ${width(className)} ${inputBase} ${className}`} {...p} />;
}

export function Switch({
  checked, onChange, disabled, label,
}: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; label: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full border transition-colors disabled:opacity-50 ${checked ? "border-allow bg-allow" : "border-line bg-sunken"}`}
    >
      <span className={`inline-block size-5 rounded-full shadow transition-transform ${checked ? "translate-x-[20px] bg-white" : "translate-x-[1px] bg-muted"}`} />
    </button>
  );
}

export function Segmented<T extends string>({
  value, onChange, options, label,
}: { value: T; onChange: (v: T) => void; options: { value: T; label: string }[]; label: string }) {
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex rounded-md border border-line bg-surface p-0.5">
      {options.map((o) => (
        <button
          key={o.value}
          role="radio"
          aria-checked={value === o.value}
          onClick={() => onChange(o.value)}
          className={`h-7 rounded px-2.5 text-xs font-medium transition-colors ${value === o.value ? "bg-primary/12 text-primary" : "text-muted hover:text-ink"}`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Tabs<T extends string>({
  value, onChange, tabs,
}: { value: T; onChange: (v: T) => void; tabs: { value: T; label: string; count?: number }[] }) {
  return (
    <div role="tablist" className="mb-5 flex gap-1 overflow-x-auto border-b border-line">
      {tabs.map((t) => (
        <button
          key={t.value}
          role="tab"
          aria-selected={value === t.value}
          onClick={() => onChange(t.value)}
          className={`-mb-px whitespace-nowrap border-b-2 px-3 py-2.5 text-sm font-medium transition-colors ${value === t.value ? "border-primary text-primary" : "border-transparent text-muted hover:text-ink"}`}
        >
          {t.label}
          {t.count != null && t.count > 0 && (
            <span className="ml-1.5 rounded-full bg-review-soft px-1.5 text-[11px] text-review-ink">{t.count}</span>
          )}
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- Feedback

export function Spinner({ label = "Yükleniyor" }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-16 text-muted" role="status">
      <Icon icon={Loading03Icon} size={20} className="animate-spin" />
      <span className="text-sm">{label}…</span>
    </div>
  );
}

export function ErrorBox({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof ApiError ? error.message : "Veriler yüklenemedi. Bağlantınızı kontrol edin.";
  return (
    <div className="flex items-center justify-between gap-3 rounded-md bg-block-soft px-4 py-3 text-sm text-block-ink" role="alert">
      <span className="flex items-center gap-2"><Icon icon={Alert02Icon} size={16} />{message}</span>
      {onRetry && <Button size="sm" onClick={onRetry}>Tekrar dene</Button>}
    </div>
  );
}

export function Empty({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center px-6 py-14 text-center">
      <p className="font-medium">{title}</p>
      {children && <p className="mt-1 max-w-[52ch] text-sm text-muted">{children}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function CopyButton({ value, label = "Kopyala" }: { value: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <Button
      size="sm"
      icon={<Icon icon={done ? Tick02Icon : Copy01Icon} size={14} />}
      onClick={async () => {
        await navigator.clipboard.writeText(value);
        setDone(true);
        setTimeout(() => setDone(false), 1600);
      }}
    >
      {done ? "Kopyalandı" : label}
    </Button>
  );
}

// ---------------------------------------------------------------- Modal

export function Modal({
  open, onClose, title, description, children, footer, width = "max-w-lg",
}: {
  open: boolean; onClose: () => void; title: string; description?: ReactNode;
  children?: ReactNode; footer?: ReactNode; width?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement as HTMLElement | null;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    const first = ref.current?.querySelector<HTMLElement>("input, select, textarea, button:not([data-close])");
    first?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      prev?.focus();
    };
  }, [open, onClose]);

  if (!open) return null;
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-overlay/60 p-3 pt-[10vh] backdrop-blur-sm" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={ref} role="dialog" aria-modal="true" aria-label={title} className={`w-full ${width} rounded-lg border border-line bg-surface shadow-xl`}>
        <div className="flex items-start justify-between gap-4 border-b border-line px-5 py-3">
          <div className="pt-1.5">
            <h2 className="text-sm font-semibold">{title}</h2>
            {description && <p className="mt-1 text-sm text-muted">{description}</p>}
          </div>
          <button data-close onClick={onClose} className="inline-flex size-9 shrink-0 items-center justify-center rounded-md text-muted hover:bg-sunken hover:text-ink" aria-label="Kapat">
            <Icon icon={Cancel01Icon} />
          </button>
        </div>
        {children && <div className="px-5 py-4">{children}</div>}
        {footer && <div className="flex flex-wrap justify-end gap-2 border-t border-line px-5 py-3">{footer}</div>}
      </div>
    </div>,
    document.body,
  );
}

/** Tek seferlik gösterilen gizli değer (API key, geçici şifre) */
export function SecretReveal({ value, warning }: { value: string; warning: string }) {
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 rounded-md border border-line bg-sunken p-3">
        <code className="min-w-0 flex-1 break-all font-mono text-[13px]">{value}</code>
        <CopyButton value={value} />
      </div>
      <p className="flex gap-2 rounded-md bg-review-soft px-3 py-2 text-sm text-review-ink">
        <Icon icon={Alert02Icon} size={16} className="mt-0.5 shrink-0" />
        {warning}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------- Table

export function Table({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] border-collapse text-sm [&_td]:border-t [&_td]:border-line [&_td]:px-5 [&_td]:py-2.5 [&_td]:align-middle [&_th]:px-5 [&_th]:py-2 [&_th]:text-left [&_th]:text-xs [&_th]:font-medium [&_th]:text-muted [&_tbody_tr:hover]:bg-sunken/50">
        {children}
      </table>
    </div>
  );
}

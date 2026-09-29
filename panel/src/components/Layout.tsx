import {
  Activity02Icon, AiBrain01Icon, Analytics01Icon, Archive02Icon, FilterIcon, Cancel01Icon, CheckListIcon, ImageNotFound01Icon, DashboardSquare01Icon, Folder01Icon, InboxIcon, Logout01Icon,
  Menu01Icon, SecurityBlockIcon, SecurityCheckIcon, Settings02Icon, UserCircleIcon, UserGroupIcon,
} from "@hugeicons/core-free-icons";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";

import { api } from "../api";
import { useAuth } from "../auth";
import { can, ROLE_LABEL } from "../format";
import { ThemeToggle } from "../theme";
import type { PlatformSettings, Role } from "../types";
import { Icon, type IconDef } from "./icon";

type NavItem = { to: string; label: string; icon: IconDef; min: Role; badge?: number };

/** Karar üçlüsü (izin / incele / engelle) — ürünün işareti */
export function BrandMark({ className = "" }: { className?: string }) {
  return (
    <span className={`inline-flex items-center gap-[3px] ${className}`} aria-hidden>
      <span className="h-2.5 w-2 rounded-[2px] bg-allow" />
      <span className="h-2.5 w-2 rounded-[2px] bg-review" />
      <span className="h-2.5 w-2 rounded-[2px] bg-block" />
    </span>
  );
}

export function Layout({ children }: { children: ReactNode }) {
  const { user, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const location = useLocation();
  useEffect(() => setOpen(false), [location.pathname]);

  const reviewSummary = useQuery({
    queryKey: ["review-summary"],
    queryFn: () => api<{ pending: number }>("/review/summary"),
    refetchInterval: 30_000,
  });
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => api<PlatformSettings>("/settings") });
  const aiAlerts = useQuery({
    queryKey: ["ai-alerts"],
    queryFn: () => api<{ alerts: { kind: string; message: string }[]; circuit: string | null }>("/ai/alerts/active"),
    refetchInterval: 60_000,
  });

  const groups: { title: string; items: NavItem[] }[] = [
    {
      title: "Moderasyon",
      items: [
        { to: "/", label: "Genel bakış", icon: DashboardSquare01Icon, min: "viewer" },
        { to: "/review", label: "İnceleme kuyruğu", icon: InboxIcon, min: "moderator", badge: reviewSummary.data?.pending },
        { to: "/decisions", label: "Kararlar", icon: CheckListIcon, min: "viewer" },
        { to: "/blocklist", label: "Görsel engel listesi", icon: ImageNotFound01Icon, min: "moderator" },
        { to: "/quality", label: "Karar kalitesi", icon: Analytics01Icon, min: "viewer" },
        { to: "/legal-holds", label: "Yasal saklama", icon: Archive02Icon, min: "moderator" },
      ],
    },
    {
      title: "Yönetim",
      items: [
        { to: "/rules", label: "Kurallar", icon: FilterIcon, min: "viewer" },
        { to: "/projects", label: "Projeler", icon: Folder01Icon, min: "viewer" },
        { to: "/ip-rules", label: "IP engelleri", icon: SecurityBlockIcon, min: "admin" },
        { to: "/settings", label: "Platform ayarları", icon: Settings02Icon, min: "viewer" },
      ],
    },
    {
      title: "Sistem",
      items: [
        { to: "/admins", label: "Yöneticiler", icon: UserGroupIcon, min: "admin" },
        { to: "/audit", label: "Denetim kaydı", icon: SecurityCheckIcon, min: "admin" },
        { to: "/ai", label: "AI ve maliyet", icon: AiBrain01Icon, min: "viewer" },
        { to: "/system", label: "Sistem durumu", icon: Activity02Icon, min: "viewer" },
      ],
    },
  ];
  const allItems = groups.flatMap((g) => g.items);
  const currentLabel =
    allItems.find((i) => (i.to === "/" ? location.pathname === "/" : location.pathname.startsWith(i.to)))?.label ??
    (location.pathname === "/account" ? "Hesabım" : "Moderasyon");

  const brand = (
    <Link to="/" className="block min-w-0">
      <span className="flex items-center gap-2 text-sm font-semibold"><BrandMark />Moderasyon</span>
      <span className="mt-0.5 block truncate text-xs text-muted">{user?.email || user?.username}</span>
    </Link>
  );

  const nav = (
    <nav className="flex-1 space-y-5 overflow-y-auto px-2 pb-4">
      {groups.map((g) => {
        const items = g.items.filter((i) => can(user?.role, i.min));
        if (!items.length) return null;
        return (
          <div key={g.title}>
            <div className="px-3 pb-1 text-xs text-faint">{g.title}</div>
            <div className="space-y-0.5">
              {items.map((i) => (
                <NavLink
                  key={i.to}
                  to={i.to}
                  end={i.to === "/"}
                  className={({ isActive }) =>
                    `flex items-center gap-2.5 rounded-md px-3 py-2 text-sm transition-colors ${isActive ? "bg-primary/10 font-medium text-primary" : "text-muted hover:bg-sunken hover:text-ink"}`
                  }
                >
                  <Icon icon={i.icon} />
                  <span className="flex-1">{i.label}</span>
                  {!!i.badge && (
                    <span className="rounded-sm bg-review-soft px-1.5 text-[11px] font-semibold tabular-nums text-review-ink">{i.badge}</span>
                  )}
                </NavLink>
              ))}
            </div>
          </div>
        );
      })}
    </nav>
  );

  const footer = (
    <div className="border-t border-line p-2">
      <NavLink
        to="/account"
        className={({ isActive }) =>
          `flex items-center gap-2.5 rounded-md px-3 py-2 text-sm transition-colors ${isActive ? "bg-primary/10 text-primary" : "text-muted hover:bg-sunken hover:text-ink"}`
        }
      >
        <Icon icon={UserCircleIcon} />
        <span className="min-w-0 flex-1 truncate">Hesabım</span>
        <span className="text-xs text-faint">{user && ROLE_LABEL[user.role]}</span>
      </NavLink>
      <div className="mt-1 flex items-center justify-between px-1">
        <ThemeToggle />
        <button
          onClick={logout}
          className="inline-flex h-8 items-center gap-2 rounded-md px-3 text-xs font-medium text-muted hover:bg-sunken hover:text-ink"
        >
          <Icon icon={Logout01Icon} size={16} />Çıkış
        </button>
      </div>
    </div>
  );

  const serviceOff = settings.data && !settings.data.service_enabled;

  return (
    <div className="flex min-h-full">
      {/* Masaüstü kenar çubuğu */}
      <aside className="sticky top-0 hidden h-screen w-60 shrink-0 flex-col border-r border-line bg-surface lg:flex">
        <div className="px-5 py-4">{brand}</div>
        {nav}
        {footer}
      </aside>

      {/* Mobil menü */}
      {open && (
        <div className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-modal="true">
          <div className="absolute inset-0 bg-overlay/60" onClick={() => setOpen(false)} />
          <aside className="absolute inset-y-0 left-0 flex w-72 flex-col border-r border-line bg-surface">
            <div className="flex items-center justify-between px-4 py-3">
              {brand}
              <button onClick={() => setOpen(false)} aria-label="Menüyü kapat" className="inline-flex size-9 items-center justify-center rounded-md hover:bg-sunken">
                <Icon icon={Cancel01Icon} />
              </button>
            </div>
            {nav}
            {footer}
          </aside>
        </div>
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex items-center gap-3 border-b border-line bg-canvas/80 px-4 py-2.5 backdrop-blur lg:hidden">
          <button onClick={() => setOpen(true)} aria-label="Menüyü aç" className="inline-flex size-9 items-center justify-center rounded-md hover:bg-sunken">
            <Icon icon={Menu01Icon} />
          </button>
          <span className="flex-1 text-sm font-semibold">{currentLabel}</span>
          <ThemeToggle />
        </header>

        {serviceOff && (
          <div className="border-b border-block/30 bg-block-soft px-4 py-2 text-sm text-block-ink sm:px-6 lg:px-10">
            <strong className="font-semibold">Hizmet kapalı.</strong> Moderasyon API'si şu anda tüm isteklere 503 dönüyor.{" "}
            {can(user?.role, "admin") && location.pathname !== "/settings" && (
              <Link to="/settings" className="font-medium underline underline-offset-2">Ayarlardan açın</Link>
            )}
          </div>
        )}

        {(aiAlerts.data?.alerts.length || aiAlerts.data?.circuit) && location.pathname !== "/ai" ? (
          <div className="border-b border-review/30 bg-review-soft px-4 py-2 text-sm text-review-ink sm:px-6 lg:px-10">
            <strong className="font-semibold">AI: </strong>
            {aiAlerts.data?.alerts[0]?.message ?? "Sağlayıcı hata verdiği için AI geçici olarak duraklatıldı; yerel modellerle devam ediliyor."}{" "}
            <Link to="/ai" className="font-medium underline underline-offset-2">Ayrıntılar</Link>
          </div>
        ) : null}

        <main className="flex-1 px-4 py-5 sm:px-6 lg:px-10 lg:py-6">
          <div className="mx-auto w-full max-w-[1400px]">{children}</div>
        </main>
      </div>
    </div>
  );
}

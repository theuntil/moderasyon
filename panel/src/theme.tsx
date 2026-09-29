import { Moon02Icon, Sun01Icon } from "@hugeicons/core-free-icons";
import { useCallback, useEffect, useState } from "react";

import { Icon } from "./components/icon";

type Theme = "light" | "dark";
const KEY = "mp-theme";

function current(): Theme {
  return document.documentElement.classList.contains("dark") ? "dark" : "light";
}

export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(current);

  const setTheme = useCallback((t: Theme) => {
    document.documentElement.classList.toggle("dark", t === "dark");
    try { localStorage.setItem(KEY, t); } catch { /* gizli sekmede depolama kapalı olabilir */ }
    setThemeState(t);
    window.dispatchEvent(new CustomEvent("theme:change", { detail: t }));
  }, []);

  useEffect(() => {
    const sync = () => setThemeState(current());
    window.addEventListener("theme:change", sync);
    return () => window.removeEventListener("theme:change", sync);
  }, []);

  return { theme, setTheme, toggle: () => setTheme(theme === "dark" ? "light" : "dark") };
}

export function ThemeToggle({ className = "" }: { className?: string }) {
  const { theme, toggle } = useTheme();
  const label = theme === "dark" ? "Açık temaya geç" : "Koyu temaya geç";
  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={label}
      title={label}
      className={`inline-flex size-9 items-center justify-center rounded-md text-muted transition-colors hover:bg-sunken hover:text-ink ${className}`}
    >
      <Icon icon={theme === "dark" ? Moon02Icon : Sun01Icon} />
    </button>
  );
}

/** Grafik renkleri: SVG özniteliklerine gerçek renk verilir, tema değişince yeniden okunur. */
export function useChartColors() {
  const { theme } = useTheme();
  const read = () => {
    const css = getComputedStyle(document.documentElement);
    const v = (name: string) => `hsl(${css.getPropertyValue(name).trim()})`;
    return {
      allow: v("--allow"), review: v("--review"), block: v("--block"), line: v("--border"),
      primary: v("--chart-line"), muted: v("--muted-foreground"), cursor: `hsl(${css.getPropertyValue("--muted").trim()})`,
    };
  };
  const [colors, setColors] = useState(read);
  useEffect(() => setColors(read()), [theme]);
  return colors;
}

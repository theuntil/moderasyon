import { CancelCircleIcon, CheckmarkCircle02Icon } from "@hugeicons/core-free-icons";
import { createContext, useCallback, useContext, useState, type ReactNode } from "react";

import { Icon } from "./icon";

type Toast = { id: number; kind: "success" | "error"; text: string };
const ToastContext = createContext<(kind: Toast["kind"], text: string) => void>(() => {});

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((kind: Toast["kind"], text: string) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, kind, text }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), kind === "error" ? 7000 : 3500);
  }, []);

  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="fixed bottom-4 right-4 z-[60] flex w-[min(380px,calc(100vw-2rem))] flex-col gap-2" aria-live="polite">
        {toasts.map((t) => (
          <div
            key={t.id}
            className="flex items-start gap-2.5 rounded-lg border border-line bg-surface px-3.5 py-3 text-sm shadow-lg"
          >
            {t.kind === "success" ? (
              <Icon icon={CheckmarkCircle02Icon} size={16} className="mt-0.5 shrink-0 text-allow" />
            ) : (
              <Icon icon={CancelCircleIcon} size={16} className="mt-0.5 shrink-0 text-block" />
            )}
            <span>{t.text}</span>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export const useToast = () => useContext(ToastContext);

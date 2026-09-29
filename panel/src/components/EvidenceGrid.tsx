import { ViewIcon, ViewOffIcon } from "@hugeicons/core-free-icons";
import { useState } from "react";

import { categoryLabel, duration } from "../format";
import type { Evidence } from "../types";
import { Icon } from "./icon";

/**
 * Kanıt görselleri. Moderatör sağlığı için varsayılan olarak bulanık gösterilir;
 * tek tek veya hepsi birden açılabilir. Görseller yetki kontrollü admin API'den gelir.
 */
export function EvidenceGrid({ items, size = "md" }: { items: Evidence[]; size?: "sm" | "md" }) {
  const [revealed, setRevealed] = useState<Set<string>>(new Set());
  const [all, setAll] = useState(false);
  if (!items.length) return null;

  const toggle = (id: string) =>
    setRevealed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const isShown = (id: string) => all || revealed.has(id);
  const cols = size === "sm" ? "grid-cols-3 sm:grid-cols-4" : items.length === 1 ? "grid-cols-1 max-w-md" : "grid-cols-2 sm:grid-cols-3";

  return (
    <div className="space-y-2">
      <div className={`grid gap-2 ${cols}`}>
        {items.map((e) => {
          const shown = isShown(e.id);
          const top = e.categories[0];
          return (
            <figure key={e.id} className="overflow-hidden rounded-md border border-line bg-sunken">
              <button
                type="button"
                onClick={() => toggle(e.id)}
                className="relative block w-full"
                aria-label={shown ? "Görseli bulanıklaştır" : "Görseli göster"}
              >
                <img
                  src={`/api/media/evidence/${e.id}`}
                  alt=""
                  loading="lazy"
                  className={`aspect-video w-full object-contain transition-[filter] ${shown ? "" : "scale-105 blur-2xl"}`}
                />
                {!shown && (
                  <span className="absolute inset-0 flex items-center justify-center gap-1.5 bg-overlay/30 text-xs font-medium text-white">
                    <Icon icon={ViewIcon} size={14} /> Göster
                  </span>
                )}
              </button>
              {(e.timestamp_ms != null || top) && (
                <figcaption className="flex items-center justify-between gap-2 px-2 py-1 text-[11px] text-muted">
                  <span className="font-mono">{e.timestamp_ms != null ? duration(e.timestamp_ms) : ""}</span>
                  {top && top.score > 0 && <span className="truncate">{categoryLabel(top.name)} · {Math.round(top.score * 100)}%</span>}
                </figcaption>
              )}
            </figure>
          );
        })}
      </div>
      {(items.length > 1 || revealed.size > 0 || all) && (
        <button type="button" onClick={() => { setAll(!all); setRevealed(new Set()); }} className="inline-flex items-center gap-1 text-xs text-muted hover:text-ink">
          <Icon icon={all || revealed.size > 0 ? ViewOffIcon : ViewIcon} size={14} />
          {all || revealed.size > 0 ? "Hepsini bulanıklaştır" : "Hepsini göster"}
        </button>
      )}
    </div>
  );
}

import { HugeiconsIcon } from "@hugeicons/react";
import type { ComponentProps } from "react";

export type IconDef = ComponentProps<typeof HugeiconsIcon>["icon"];

/** Hugeicons (ücretsiz, stroke rounded) sarmalayıcı. Varsayılan 18px, 1.5 çizgi, currentColor. */
export function Icon({
  icon, size = 18, className, strokeWidth = 1.5,
}: { icon: IconDef; size?: number; className?: string; strokeWidth?: number }) {
  return <HugeiconsIcon icon={icon} size={size} strokeWidth={strokeWidth} className={className} aria-hidden />;
}

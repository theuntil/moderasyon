import { DECISION_LABEL, num, pct } from "../format";

type Counts = { allow: number; review: number; block: number };

const SEGMENTS = [
  { key: "allow", bar: "bg-allow", dot: "bg-allow" },
  { key: "review", bar: "bg-review", dot: "bg-review" },
  { key: "block", bar: "bg-block", dot: "bg-block" },
] as const;

/** Panelin imza öğesi: kararların oransal dağılımı tek bir şeritte. */
export function VerdictStrip({
  counts, size = "sm", legend = false, animate = false,
}: { counts: Counts; size?: "sm" | "lg"; legend?: boolean; animate?: boolean }) {
  const total = counts.allow + counts.review + counts.block;
  const height = size === "lg" ? "h-4" : "h-1.5";

  return (
    <div>
      <div
        className={`flex w-full gap-[2px] overflow-hidden rounded-full bg-sunken ${height} ${animate ? "strip-animate" : ""}`}
        role="img"
        aria-label={SEGMENTS.map((s) => `${DECISION_LABEL[s.key]}: ${num(counts[s.key])}`).join(", ")}
      >
        {total > 0 &&
          SEGMENTS.map((s) =>
            counts[s.key] > 0 ? (
              <div key={s.key} className={s.bar} style={{ flexGrow: counts[s.key], minWidth: 3 }} />
            ) : null,
          )}
      </div>
      {legend && (
        <dl className="mt-4 grid grid-cols-3 gap-4">
          {SEGMENTS.map((s) => (
            <div key={s.key}>
              <dt className="flex items-center gap-2 text-xs text-muted">
                <span className={`size-2 rounded-full ${s.dot}`} />
                {DECISION_LABEL[s.key]}
              </dt>
              <dd className="mt-1 flex items-baseline gap-2">
                <span className="text-2xl font-semibold tabular-nums">{num(counts[s.key])}</span>
                <span className="text-xs text-muted">{pct(counts[s.key], total)}</span>
              </dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}

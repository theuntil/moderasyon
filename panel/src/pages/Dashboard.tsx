import { useState } from "react";

import { OverviewView, RANGE_OPTIONS } from "../components/Overview";
import { PageHeader, Segmented } from "../components/ui";
import type { RangeKey } from "../types";

export function DashboardPage() {
  const [range, setRange] = useState<RangeKey>("7d");
  return (
    <>
      <PageHeader
        title="Genel bakış"
        description="Tüm projelerin moderasyon trafiği. Veriler 30 saniyede bir yenilenir."
        actions={<Segmented label="Zaman aralığı" value={range} onChange={setRange} options={RANGE_OPTIONS} />}
      />
      <OverviewView range={range} />
    </>
  );
}

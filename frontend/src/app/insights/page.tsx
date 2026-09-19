import { computeSparklineCoords, type PriceHistoryPoint } from "@/lib/sparkline";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";
const TENANT_ID = process.env.NEXT_PUBLIC_DEV_TENANT_ID ?? "";

type BenchmarkPosition = {
  p25: string;
  p50: string;
  p75: string;
  tenant_price: string;
  distinct_tenant_count: number;
  scope: string;
};
type InsightCard = {
  alert_id: string;
  canonical_sku_id: string;
  canonical_sku_name: string;
  alert_type: string;
  baseline_price: string;
  current_price: string;
  pct_change: string;
  window_start: string;
  window_end: string;
  status: string;
  price_history: PriceHistoryPoint[];
  benchmark: BenchmarkPosition | null;
};

async function getInsights(): Promise<InsightCard[]> {
  if (!TENANT_ID) return [];
  const res = await fetch(`${API_BASE}/insights?tenant_id=${TENANT_ID}`, { cache: "no-store" });
  if (!res.ok) return [];
  return res.json();
}

function Sparkline({ points }: { points: PriceHistoryPoint[] }) {
  if (points.length < 2) return <span className="text-xs text-gray-400">not enough history</span>;

  const width = 220;
  const height = 40;
  const coords = computeSparklineCoords(points, width, height);

  return (
    <svg width={width} height={height} className="overflow-visible">
      <polyline points={coords.join(" ")} fill="none" stroke="#2563eb" strokeWidth={1.5} />
    </svg>
  );
}

function BenchmarkBar({ benchmark }: { benchmark: BenchmarkPosition }) {
  const values = [benchmark.p25, benchmark.p50, benchmark.p75, benchmark.tenant_price].map(Number);
  const max = Math.max(...values) * 1.05;
  const tenantPct = (Number(benchmark.tenant_price) / max) * 100;
  const p50Pct = (Number(benchmark.p50) / max) * 100;

  return (
    <div className="mt-1">
      <div className="relative h-2 w-56 rounded bg-gray-200">
        <div className="absolute top-0 h-2 w-0.5 bg-gray-500" style={{ left: `${p50Pct}%` }} title="peer median" />
        <div
          className="absolute top-0 h-2 w-0.5 bg-red-600"
          style={{ left: `${tenantPct}%` }}
          title="your price"
        />
      </div>
      <div className="mt-1 text-xs text-gray-500">
        Peer p25 ${benchmark.p25} · p50 ${benchmark.p50} · p75 ${benchmark.p75} ({benchmark.scope},{" "}
        {benchmark.distinct_tenant_count} tenants) — you: ${benchmark.tenant_price}
      </div>
    </div>
  );
}

export default async function InsightsPage() {
  const cards = await getInsights();

  if (!TENANT_ID) {
    return (
      <p className="text-sm text-gray-600">
        Set <code>NEXT_PUBLIC_DEV_TENANT_ID</code> in <code>frontend/.env.local</code>.
      </p>
    );
  }

  return (
    <div className="max-w-3xl">
      <h1 className="mb-4 text-xl font-semibold">Insights</h1>
      {cards.length === 0 && <p className="text-sm text-gray-500">No open alerts right now.</p>}
      <div className="space-y-4">
        {cards.map((card) => (
          <div key={card.alert_id} className="rounded border border-gray-200 bg-white p-4">
            <div className="flex items-start justify-between">
              <div>
                <div className="text-sm uppercase tracking-wide text-amber-600">{card.alert_type}</div>
                <div className="text-lg font-medium">{card.canonical_sku_name}</div>
                <div className="text-sm text-gray-600">
                  ${card.baseline_price} &rarr; ${card.current_price} (
                  {(Number(card.pct_change) * 100).toFixed(1)}%) since {card.window_start}
                </div>
              </div>
              <Sparkline points={card.price_history} />
            </div>
            {card.benchmark && <BenchmarkBar benchmark={card.benchmark} />}
          </div>
        ))}
      </div>
    </div>
  );
}

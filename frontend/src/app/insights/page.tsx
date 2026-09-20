import { computeSparklineCoords, type PriceHistoryPoint } from "@/lib/sparkline";
import { labelAnchor, ordinal, priceVerdict, trackPosition } from "@/lib/spectrum";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";
const TENANT_ID = process.env.NEXT_PUBLIC_DEV_TENANT_ID ?? "";

type BenchmarkPosition = {
  p25: string;
  p50: string;
  p75: string;
  tenant_price: string;
  percentile: string;
  distinct_account_count: number;
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

const TONE = {
  good: { text: "text-emerald-700", pin: "#047857" },
  fair: { text: "text-amber-700", pin: "#b45309" },
  bad: { text: "text-red-700", pin: "#b91c1c" },
} as const;

const QUARTILE_TICKS = [0.25, 0.5, 0.75] as const;

const LABEL_TRANSFORM = {
  start: "translateX(-0.35rem)",
  middle: "translateX(-50%)",
  end: "translateX(calc(-100% + 0.35rem))",
} as const;

/** Where this price sits among comparable businesses, as a spectrum.
 *
 * Replaces a 2px-tick bar that required reading four dollar figures and doing
 * the comparison in your head. The question a reader actually has is "is this
 * bad?", so the percentile answers it in words first and the track shows the
 * distribution behind that answer.
 */
function BenchmarkSpectrum({ benchmark }: { benchmark: BenchmarkPosition }) {
  const percentile = Number(benchmark.percentile);
  const verdict = priceVerdict(percentile, benchmark.distinct_account_count);
  const tone = TONE[verdict.tone];
  const quartilePrices = [benchmark.p25, benchmark.p50, benchmark.p75];
  const pinPosition = trackPosition(percentile);

  return (
    <div className="mt-3">
      <div className={`text-sm font-medium ${tone.text}`}>
        {verdict.headline} · {ordinal(percentile * 100)} percentile
      </div>

      <div className="relative mt-2 h-11">
        {/* Cheap on the left, dear on the right. Fixed stops, because the axis
            is percentile: the colour under the pin always means the same
            thing, on this card and on every other one. */}
        <div
          className="absolute top-5 h-2.5 w-full rounded-full"
          style={{ background: "linear-gradient(to right, #6ee7b7 0%, #fcd34d 50%, #fca5a5 100%)" }}
        />
        {/* The middle half of the market: between the quartile ticks. */}
        <div
          className="absolute top-5 h-2.5 bg-black/5"
          style={{
            left: `${trackPosition(0.25)}%`,
            width: `${trackPosition(0.75) - trackPosition(0.25)}%`,
          }}
        />
        {QUARTILE_TICKS.map((q) => (
          <div
            key={q}
            className="absolute top-[18px] h-[18px] w-px bg-gray-500/60"
            style={{ left: `${trackPosition(q)}%` }}
          />
        ))}

        {/* The reader's own price. Labelled as well as coloured — the position
            has to survive being printed in greyscale or read colour-blind.
            Label and pin are positioned separately so the label can hug the
            edge without dragging the pin off its percentile. */}
        <span
          className={`absolute top-0 whitespace-nowrap text-xs font-semibold ${tone.text}`}
          style={{ left: `${pinPosition}%`, transform: LABEL_TRANSFORM[labelAnchor(percentile)] }}
        >
          you ${benchmark.tenant_price}
        </span>
        <svg
          width="11"
          height="8"
          viewBox="0 0 11 8"
          aria-hidden="true"
          className="absolute top-[14px] -translate-x-1/2"
          style={{ left: `${pinPosition}%` }}
        >
          <path d="M5.5 8 L0 0 L11 0 Z" fill={tone.pin} />
        </svg>
      </div>

      {/* Dollar values under their own ticks, so the percentile axis still
          answers "and what would paying like them actually cost?". */}
      <div className="relative mt-1 h-4">
        {QUARTILE_TICKS.map((q, i) => (
          <span
            key={q}
            className="absolute -translate-x-1/2 whitespace-nowrap text-[11px] text-gray-500"
            style={{ left: `${trackPosition(q)}%` }}
          >
            ${quartilePrices[i]}
          </span>
        ))}
      </div>

      <div className="mt-1 text-xs text-gray-400">
        {/* "businesses", not "tenants": a multi-unit group counts once, so this
            is how many independent operators stand behind the cell. */}
        25th / 50th / 75th percentile across {benchmark.distinct_account_count} comparable businesses (
        {benchmark.scope})
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
            {/* Stacked on a phone: the sparkline is a fixed 220px, so sitting
                it beside the text crushed that column to ~140px and wrapped
                the SKU name and the price line onto six lines apiece. */}
            <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
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
            {card.benchmark && <BenchmarkSpectrum benchmark={card.benchmark} />}
          </div>
        ))}
      </div>
    </div>
  );
}

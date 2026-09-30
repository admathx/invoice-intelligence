import { computeSparklineCoords, type PriceHistoryPoint } from "@/lib/sparkline";
import { labelAnchor, priceVerdict, trackPosition } from "@/lib/spectrum";

import ActionButton from "@/components/ActionButton";
import NoLocation from "@/components/NoLocation";
import { percent, priceChangeTone, TONE_TEXT, unitPrice } from "@/lib/format";
import { requireSession, serverGet } from "@/lib/server";

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
  distributor_name: string | null;
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

async function getInsights(locationId: string): Promise<InsightCard[]> {
  const res = await serverGet(`/insights?tenant_id=${locationId}`);
  if (!res.ok) return [];
  return res.json();
}

function Sparkline({ points }: { points: PriceHistoryPoint[] }) {
  if (points.length < 2) return <span className="text-xs text-gray-400">Not enough history for a chart yet</span>;

  const width = 220;
  const height = 40;
  const coords = computeSparklineCoords(points, width, height);

  return (
    <svg width={width} height={height} className="overflow-visible">
      {/* Red: every card here is a price that went up. */}
      <polyline points={coords.join(" ")} fill="none" stroke="#dc2626" strokeWidth={2} strokeLinejoin="round" />
    </svg>
  );
}

const ALERT_TYPE: Record<string, string> = {
  creep: "Price going up",
  off_contract: "Off your usual price",
  above_peer: "Above what others pay",
};

const TONE = {
  good: { text: "text-brand-700", pin: "#15803d" },
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
        {verdict.headline}
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
          you {unitPrice(benchmark.tenant_price)}
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
            {unitPrice(quartilePrices[i])}
          </span>
        ))}
      </div>

      <div className="mt-1 text-xs text-gray-400">
        {/* "businesses", not "tenants": a multi-unit group counts once, so this
            is how many independent operators stand behind the cell. */}
        Low, typical and high prices paid by {benchmark.distinct_account_count} similar businesses{" "}
        {benchmark.scope === "metro" ? "in your area" : "nationwide"}
      </div>
    </div>
  );
}

export default async function InsightsPage() {
  const { locationId } = await requireSession();
  if (!locationId) return <NoLocation />;
  const cards = await getInsights(locationId);

  return (
    <div className="max-w-3xl">
      <div className="mb-4">
        <div className="flex flex-wrap items-baseline gap-3">
          <h1 className="page-title">Price alerts</h1>
          {cards.length > 0 && (
            <span className="badge bg-red-100 text-red-700">
              {cards.length} price increase{cards.length === 1 ? "" : "s"}
            </span>
          )}
        </div>
        <p className="mt-0.5 text-sm text-gray-500">
          Products you&rsquo;re now paying noticeably more for. Worth raising with your rep; your{" "}
          <a href="/negotiation" className="link">
            savings
          </a>{" "}
          list has the numbers to bring.
        </p>
      </div>
      {cards.length === 0 && (
        <div className="card flex items-center gap-3 border-l-4 border-l-brand-400 px-4 py-4 text-sm text-gray-700">
          <span aria-hidden className="grid h-8 w-8 place-items-center rounded-full bg-brand-100 text-brand-800">
            ✓
          </span>
          No price increases right now. Prices are holding steady.
        </div>
      )}
      <div className="space-y-4">
        {cards.map((card) => (
          <div key={card.alert_id} className="card border-l-4 border-l-red-400 p-5">
            {/* Stacked on a phone: the sparkline is a fixed 220px, so sitting
                it beside the text crushed that column to ~140px and wrapped
                the SKU name and the price line onto six lines apiece. */}
            <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
              <div>
                <span className="badge bg-amber-100 text-amber-800">{ALERT_TYPE[card.alert_type] ?? "Price change"}</span>
                <div className="mt-1 text-lg font-semibold">
                  <a href={`/skus/${card.canonical_sku_id}`} className="hover:text-brand-700 hover:underline">
                    {card.canonical_sku_name}
                  </a>
                  {card.distributor_name && (
                    <span className="font-normal text-gray-500"> from {card.distributor_name}</span>
                  )}
                </div>
                <div className="num mt-0.5 flex flex-wrap items-center gap-2 text-sm text-gray-600">
                  <span>
                    {unitPrice(card.baseline_price)} &rarr;{" "}
                    <strong className="text-gray-900">{unitPrice(card.current_price)}</strong>
                  </span>
                  <span className={`badge bg-red-100 ${TONE_TEXT[priceChangeTone(card.pct_change)]}`}>
                    ▲ {percent(card.pct_change, { signed: true })}
                  </span>
                  <span className="text-gray-500">since {card.window_start}</span>
                </div>
              </div>
              <Sparkline points={card.price_history} />
            </div>
            {card.benchmark && <BenchmarkSpectrum benchmark={card.benchmark} />}
            <div className="mt-3 flex justify-end border-t border-gray-100 pt-2">
              <ActionButton
                label="Dealt with it"
                question={`Take ${card.canonical_sku_name}${card.distributor_name ? ` from ${card.distributor_name}` : ""} off your price alerts? We'll tell you again if the price goes up more.`}
                path={`/insights/${card.alert_id}/dismiss?tenant_id=${locationId}`}
              />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

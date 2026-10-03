"use client";

import { useMemo, useState } from "react";

import StatTile from "@/components/StatTile";
import {
  byCategory,
  byDistributor,
  categoryLabel,
  days,
  dollars,
  planned,
  recentAsSliders,
  signedDollars,
  signedPercent,
  type Costs,
  type Scenario,
  type ScenarioKey,
} from "@/lib/costs";
import { percent, priceChangeTone, TONE_TEXT } from "@/lib/format";
import { useHydrated } from "@/lib/useHydrated";

// How far a slider goes. Prices: further up than down, as prices go. Wide
// enough for a bad year, narrow enough that a half point is easy to land on.
const PRICE = { min: -20, max: 30, step: 0.5 };
const VOLUME = { min: -30, max: 30, step: 1 };
const TOP_PRODUCTS = 10;

// What each ready-made scenario assumes, and where to act on it.
const SCENARIOS: Record<ScenarioKey, { label: (window: number) => string; about: (n: number) => string; href?: string; link?: string }> = {
  increases_reversed: {
    label: () => "Your flagged prices go back to what they were",
    about: (n) => `${n} product${n === 1 ? "" : "s"} on Price alerts`,
    href: "/insights",
    link: "See price alerts",
  },
  cheaper_elsewhere: {
    label: () => "You get the cheapest price found at another distributor",
    about: (n) => `${n} flagged product${n === 1 ? "" : "s"}, by a match or by moving`,
    href: "/insights",
    link: "See where",
  },
  savings_targets: {
    label: () => "You get every target price on Savings",
    about: (n) => `${n} product${n === 1 ? "" : "s"} with a price to ask for`,
    href: "/negotiation",
    link: "See savings",
  },
  rise_again: {
    label: (window) => `Prices move again like the last ${days(window)}`,
    about: (n) => `Each of ${n} product${n === 1 ? "" : "s"} repeats its own change`,
  },
};

const th = "th";
const cell = "py-2 pr-2 sm:pr-4";
// Columns a phone does without, so the sliders and the answer stay on screen.
const wide = "hidden sm:table-cell";

/** The Costs page: a year at today's prices, ready-made scenarios, and
 *  sliders to try one's own (backend app/analytics/costs.py). */
export default function CostPlanner({ costs }: { costs: Costs }) {
  const categories = useMemo(() => byCategory(costs.products), [costs.products]);
  const distributors = useMemo(() => byDistributor(costs.products), [costs.products]);
  const [prices, setPrices] = useState<Record<string, number>>({});
  const [volume, setVolume] = useState(0);
  // Until the page is interactive a press does nothing, and looks as if the
  // planner were broken: the controls wait, as the forms do.
  const ready = useHydrated();

  const now = Number(costs.yearly_cost);
  const plan = useMemo(() => planned(categories, prices, volume), [categories, prices, volume]);
  const untouched = volume === 0 && Object.values(prices).every((p) => p === 0);
  const all = (value: number) => Object.fromEntries(categories.map((c) => [c.name, value]));

  return (
    <>
      <div className="mb-6 grid gap-3 sm:grid-cols-3">
        <StatTile label="A year at today's prices" value={dollars(now)} tone="text-brand-700">
          What you buy now, at what it costs now
        </StatTile>
        <StatTile label="Based on" value={days(costs.window_days)}>
          of invoices, up to {costs.window_end}
        </StatTile>
        <StatTile label="Covers" value={costs.coverage === null ? "—" : percent(costs.coverage, { places: 0 })}>
          of what you spent. Items not{" "}
          <a href="/review" className="link">
            matched
          </a>{" "}
          yet, and fees, are left out.
        </StatTile>
      </div>

      <section className="mb-8">
        <h2 className="section-title">What could change it</h2>
        <p className="mb-2 mt-0.5 text-sm text-gray-500">
          Each row is its own what-if. They overlap, so don&rsquo;t add them together.
        </p>
        <div className="card overflow-x-auto">
          <table className="w-full border-collapse text-sm" data-testid="scenarios">
            <thead className="bg-gray-50">
              <tr className="border-b border-gray-200">
                <th className={`${th} pl-4`}>If&hellip;</th>
                <th className={`${th} text-right`}>Change a year</th>
                <th className={`${th} ${wide} pr-4 text-right`}>A year would cost</th>
              </tr>
            </thead>
            <tbody>
              {costs.scenarios.map((s) => (
                <ScenarioRow key={s.key} scenario={s} now={now} days={costs.window_days} />
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="mb-8">
        <h2 className="section-title">Try your own</h2>
        <p className="mb-2 mt-0.5 text-sm text-gray-500">
          Move a category&rsquo;s prices, or how much you buy, and see what the year comes to.
        </p>
        <div className="card p-4">
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              className="btn-secondary btn-sm"
              disabled={!ready}
              onClick={() => setPrices(recentAsSliders(categories, PRICE.min, PRICE.max))}
            >
              Repeat the last {days(costs.window_days)}
            </button>
            <button type="button" className="btn-secondary btn-sm" disabled={!ready} onClick={() => setPrices(all(5))}>
              Everything up 5%
            </button>
            <button type="button" className="btn-secondary btn-sm" disabled={!ready} onClick={() => setPrices(all(-3))}>
              Everything down 3%
            </button>
            <button
              type="button"
              className="btn-secondary btn-sm"
              disabled={!ready || untouched}
              onClick={() => {
                setPrices({});
                setVolume(0);
              }}
            >
              Reset
            </button>
          </div>

          <div className="mt-4 overflow-x-auto">
            <table className="w-full border-collapse text-sm" data-testid="planner">
              <thead>
                <tr className="border-b border-gray-200">
                  <th className={th}>Category</th>
                  <th className={`${th} ${wide} text-right`}>A year now</th>
                  <th className={`${th} ${wide} text-right`}>Last {days(costs.window_days)}</th>
                  <th className={th}>Price change</th>
                  <th className={`${th} ${wide} text-right`}>A year then</th>
                  <th className={`${th} text-right`}>Change</th>
                </tr>
              </thead>
              <tbody>
                {plan.rows.map((row, i) => {
                  const category = categories[i];
                  const value = prices[row.name] ?? 0;
                  return (
                    <tr key={row.name} className="border-b border-gray-100">
                      <td className={`${cell} font-medium text-gray-900`}>{categoryLabel(row.name)}</td>
                      <td className={`${cell} ${wide} num text-right`}>{dollars(row.now)}</td>
                      <td className={`${cell} ${wide} num text-right ${TONE_TEXT[priceChangeTone(category.recentChange)]}`}>
                        {percent(category.recentChange, { signed: true })}
                      </td>
                      <td className={cell}>
                        <Slider
                          label={`${categoryLabel(row.name)} price change`}
                          value={value}
                          {...PRICE}
                          disabled={!ready}
                          onChange={(v) => setPrices((p) => ({ ...p, [row.name]: v }))}
                        />
                      </td>
                      <td className={`${cell} ${wide} num text-right`}>{dollars(row.then)}</td>
                      <td className={`py-2 num text-right font-medium ${TONE_TEXT[priceChangeTone(Math.round(row.change))]}`}>
                        {signedDollars(row.change)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
              <tfoot>
                <tr className="border-b border-gray-200">
                  <td className={`${cell} font-medium text-gray-900`}>How much you buy</td>
                  <td className={wide} colSpan={2} />
                  <td className={cell}>
                    <Slider label="How much you buy" value={volume} {...VOLUME} disabled={!ready} onChange={setVolume} />
                  </td>
                  <td className={wide} />
                  <td />
                </tr>
                <tr className="font-semibold text-gray-900">
                  <td className={cell}>Total</td>
                  <td className={`${cell} ${wide} num text-right`}>{dollars(plan.now)}</td>
                  <td className={wide} />
                  <td />
                  <td className={`${cell} ${wide} num text-right`}>{dollars(plan.then)}</td>
                  <td className={`py-2 num text-right ${TONE_TEXT[priceChangeTone(Math.round(plan.change))]}`}>
                    {signedDollars(plan.change)}
                  </td>
                </tr>
              </tfoot>
            </table>
          </div>

          <p role="status" className="mt-3 text-sm text-gray-700" data-testid="planned-total">
            {untouched ? (
              "Nothing moved yet: this is the year at today's prices."
            ) : (
              <>
                The year would cost <strong>{dollars(plan.then)}</strong>:{" "}
                <strong className={TONE_TEXT[priceChangeTone(Math.round(plan.change))]}>
                  {signedDollars(plan.change)}
                </strong>{" "}
                ({signedPercent(plan.now > 0 ? Math.round((plan.change / plan.now) * 1000) / 10 : 0)}), or about{" "}
                {signedDollars(plan.change / 12)} a month.
              </>
            )}
          </p>
        </div>
      </section>

      <section>
        <h2 className="section-title">Where the money goes</h2>
        <div className="mt-2 grid items-start gap-4 lg:grid-cols-5">
          <div className="card overflow-x-auto lg:col-span-2">
            <table className="w-full border-collapse text-sm">
              <thead className="bg-gray-50">
                <tr className="border-b border-gray-200">
                  <th className={`${th} pl-4`}>Distributor</th>
                  <th className={`${th} text-right`}>A year</th>
                  <th className={`${th} pr-4 text-right`}>Share</th>
                </tr>
              </thead>
              <tbody>
                {distributors.map((d) => (
                  <tr key={d.name} className="border-b border-gray-100 last:border-0">
                    <td className="py-2 pl-4 pr-4">{d.name}</td>
                    <td className={`${cell} num text-right`}>{dollars(d.yearlyCost)}</td>
                    <td className="num py-2 pr-4 text-right text-gray-600">{percent(d.share, { places: 0 })}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="card overflow-x-auto lg:col-span-3">
            <table className="w-full border-collapse text-sm">
              <thead className="bg-gray-50">
                <tr className="border-b border-gray-200">
                  <th className={`${th} pl-4`}>Biggest products</th>
                  <th className={`${th} ${wide}`}>From</th>
                  <th className={`${th} text-right`}>A year</th>
                  <th className={`${th} pr-4 text-right`}>Share</th>
                </tr>
              </thead>
              <tbody>
                {costs.products.slice(0, TOP_PRODUCTS).map((p) => (
                  <tr key={`${p.canonical_sku_id}-${p.distributor}`} className="border-b border-gray-100 last:border-0">
                    <td className="py-2 pl-4 pr-2 sm:pr-4">
                      <a href={`/skus/${p.canonical_sku_id}`} className="link">
                        {p.name}
                      </a>
                    </td>
                    <td className={`${cell} ${wide} text-gray-600`}>{p.distributor}</td>
                    <td className={`${cell} num text-right`}>{dollars(Number(p.yearly_cost))}</td>
                    <td className="num py-2 pr-4 text-right text-gray-600">
                      {percent(now > 0 ? Number(p.yearly_cost) / now : 0, { places: 0 })}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    </>
  );
}

function ScenarioRow({ scenario, now, days }: { scenario: Scenario; now: number; days: number }) {
  const words = SCENARIOS[scenario.key];
  if (!words) return null; // one this version of the page doesn't know
  const change = Number(scenario.yearly_change);
  const nothing = scenario.products === 0;
  return (
    <tr className="border-b border-gray-100 last:border-0">
      <td className="py-2.5 pl-4 pr-4">
        <div className="font-medium text-gray-900">{words.label(days)}</div>
        <div className="text-xs text-gray-500">
          {nothing ? "Nothing to change right now" : words.about(scenario.products)}
          {words.href && !nothing && (
            <>
              {" · "}
              <a href={words.href} className="link">
                {words.link}
              </a>
            </>
          )}
        </div>
      </td>
      <td className={`${cell} num whitespace-nowrap text-right font-medium ${TONE_TEXT[priceChangeTone(Math.round(change))]}`}>
        {nothing ? "—" : signedDollars(change)}
      </td>
      <td className={`${wide} num whitespace-nowrap py-2.5 pr-4 text-right`}>{nothing ? "—" : dollars(now + change)}</td>
    </tr>
  );
}

/** A slider with its value beside it, signed. */
function Slider({
  label,
  value,
  min,
  max,
  step,
  disabled,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  disabled: boolean;
  onChange: (value: number) => void;
}) {
  return (
    <span className="flex items-center gap-2">
      <input
        type="range"
        aria-label={label}
        aria-valuetext={signedPercent(value)}
        min={min}
        max={max}
        step={step}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full min-w-[4.5rem] max-w-[14rem] accent-brand-600"
      />
      <span className={`num w-12 flex-none text-right text-sm font-medium ${TONE_TEXT[priceChangeTone(value)]}`}>
        {signedPercent(value)}
      </span>
    </span>
  );
}

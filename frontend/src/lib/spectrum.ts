/** Geometry and wording for the peer-price spectrum on the Insights page.
 *
 * Kept out of the component and unit-tested for the same reason sparkline.ts
 * is: these numbers decide what a reader concludes about their own price, and
 * a marker in the wrong place is a wrong answer that looks authoritative.
 *
 * The track is a PERCENTILE axis, not a dollar axis. A dollar axis has to be
 * rescaled per SKU, which makes every card's quartile ticks land somewhere
 * different and quietly defeats the thing a reader most wants to do — glance
 * down a page of alerts and see which ones are bad. On a percentile axis the
 * quartile ticks are in the same place on every card, so the pin's position
 * means the same thing every time. Magnitude hasn't gone anywhere: the card
 * still states the price, the move that triggered the alert, and the quartile
 * dollars under the ticks.
 */

/** Margin at each end so a price at the 0th or 100th percentile still renders
 *  as a mark on the track rather than as a sliver clipped at the edge. */
export const TRACK_INSET_PCT = 3;

/** Maps a 0-1 percentile to its position along the track, 0-100. */
export function trackPosition(fraction: number): number {
  const safe = Number.isFinite(fraction) ? fraction : 0.5;
  const clamped = Math.min(1, Math.max(0, safe));
  return TRACK_INSET_PCT + clamped * (100 - 2 * TRACK_INSET_PCT);
}

export type LabelAnchor = "start" | "middle" | "end";

/** How to hang the "you $X" label off its pin.
 *
 * Centred on the pin everywhere except near the ends, where a centred label
 * overflows the card — at the 100th percentile, which is exactly the case a
 * reader most needs to be able to read, the price was running off the right
 * edge.
 */
export function labelAnchor(fraction: number): LabelAnchor {
  const safe = Number.isFinite(fraction) ? fraction : 0.5;
  if (safe >= 0.85) return "end";
  if (safe <= 0.15) return "start";
  return "middle";
}

/** "82nd", "1st", "13th" — the percentile as a reader would say it aloud. */
export function ordinal(n: number): string {
  const rounded = Math.round(n);
  const lastTwo = rounded % 100;
  if (lastTwo >= 11 && lastTwo <= 13) return `${rounded}th`;
  switch (rounded % 10) {
    case 1:
      return `${rounded}st`;
    case 2:
      return `${rounded}nd`;
    case 3:
      return `${rounded}rd`;
    default:
      return `${rounded}th`;
  }
}

export type PriceVerdict = {
  tone: "good" | "fair" | "bad";
  /** Plain-language reading of the position — the answer to "is this bad?". */
  headline: string;
};

/** Turns a percentile (0-1 from the API) into the sentence the card leads with.
 *  Thresholds are the quartiles the track already draws, so the words and the
 *  picture can never disagree. */
export function priceVerdict(percentile: number, accountCount: number): PriceVerdict {
  const pct = Math.round(percentile * 100);
  const peers = `${accountCount} comparable ${accountCount === 1 ? "business" : "businesses"}`;

  // The extremes get named rather than expressed as a percentage: "higher than
  // 100% of 12 businesses" is a sentence no one says, and it reads like a
  // rounding artifact rather than the flat statement it actually is.
  if (pct >= 100) return { tone: "bad", headline: `More expensive than all ${peers}` };
  if (pct <= 0) return { tone: "good", headline: `Cheaper than all ${peers}` };
  if (pct <= 25) return { tone: "good", headline: `Cheaper than ${100 - pct}% of ${peers}` };
  if (pct <= 75) return { tone: "fair", headline: `Mid-pack among ${peers}` };
  return { tone: "bad", headline: `More expensive than ${pct}% of ${peers}` };
}

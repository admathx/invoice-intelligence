export type PriceHistoryPoint = { observed_on: string; unit_price_base: string };

/** Maps price history to (x,y) coordinates for an SVG polyline, normalized
 * into [0, width] x [margin, height - margin]. Pure so it's unit-testable
 * without a DOM.
 */
export function computeSparklineCoords(
  points: PriceHistoryPoint[],
  width: number,
  height: number,
  margin = 2
): string[] {
  if (points.length < 2) return [];

  const prices = points.map((p) => Number(p.unit_price_base));
  const min = Math.min(...prices);
  const max = Math.max(...prices);
  const span = max - min || 1;

  return points.map((p, i) => {
    const x = (i / (points.length - 1)) * (width - margin * 2) + margin;
    const y = height - margin - ((Number(p.unit_price_base) - min) / span) * (height - margin * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
}

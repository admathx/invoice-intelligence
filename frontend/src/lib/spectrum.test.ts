import { describe, expect, it } from "vitest";

import { TRACK_INSET_PCT, labelAnchor, ordinal, priceVerdict, trackPosition } from "./spectrum";

describe("trackPosition", () => {
  it("keeps the extremes visible instead of clipping them at the edge", () => {
    expect(trackPosition(0)).toBe(TRACK_INSET_PCT);
    expect(trackPosition(1)).toBe(100 - TRACK_INSET_PCT);
  });

  it("puts the median at the centre and the quartiles symmetrically around it", () => {
    expect(trackPosition(0.5)).toBe(50);
    expect(trackPosition(0.5) - trackPosition(0.25)).toBeCloseTo(trackPosition(0.75) - trackPosition(0.5));
  });

  it("is monotonic, so a dearer price never renders further left", () => {
    const positions = [0, 0.1, 0.25, 0.4, 0.5, 0.6, 0.75, 0.9, 1].map(trackPosition);
    for (let i = 1; i < positions.length; i++) {
      expect(positions[i]).toBeGreaterThan(positions[i - 1]);
    }
  });

  it("clamps rather than escaping the track on out-of-range or missing input", () => {
    expect(trackPosition(-0.5)).toBe(TRACK_INSET_PCT);
    expect(trackPosition(2)).toBe(100 - TRACK_INSET_PCT);
    expect(trackPosition(Number.NaN)).toBe(50);
  });
});

describe("ordinal", () => {
  it("reads percentiles the way a person says them", () => {
    expect(ordinal(1)).toBe("1st");
    expect(ordinal(2)).toBe("2nd");
    expect(ordinal(3)).toBe("3rd");
    expect(ordinal(4)).toBe("4th");
    expect(ordinal(82)).toBe("82nd");
  });

  it("handles the teens, which break the last-digit rule", () => {
    expect(ordinal(11)).toBe("11th");
    expect(ordinal(12)).toBe("12th");
    expect(ordinal(13)).toBe("13th");
  });
});

describe("priceVerdict", () => {
  it("changes tone at the same quartiles the track draws", () => {
    expect(priceVerdict(0.1, 12).tone).toBe("good");
    expect(priceVerdict(0.25, 12).tone).toBe("good");
    expect(priceVerdict(0.26, 12).tone).toBe("fair");
    expect(priceVerdict(0.75, 12).tone).toBe("fair");
    expect(priceVerdict(0.76, 12).tone).toBe("bad");
  });

  it("names the extremes instead of saying 'higher than 100%'", () => {
    expect(priceVerdict(1, 12).headline).toBe("More expensive than all 12 comparable businesses");
    expect(priceVerdict(0, 12).headline).toBe("Cheaper than all 12 comparable businesses");
    expect(priceVerdict(0.91, 11).headline).toContain("91%");
  });

  it("says how many businesses stand behind the comparison", () => {
    expect(priceVerdict(0.9, 12).headline).toContain("12 comparable businesses");
    expect(priceVerdict(0.9, 1).headline).toContain("1 comparable business");
  });
});

describe("labelAnchor", () => {
  it("centres the label away from the ends", () => {
    expect(labelAnchor(0.5)).toBe("middle");
    expect(labelAnchor(0.3)).toBe("middle");
    expect(labelAnchor(0.7)).toBe("middle");
  });

  it("hugs the edge where a centred label would overflow the card", () => {
    // The 100th percentile is the case a reader most needs to be able to
    // read, and it was the one running off the right edge.
    expect(labelAnchor(1)).toBe("end");
    expect(labelAnchor(0.9)).toBe("end");
    expect(labelAnchor(0)).toBe("start");
    expect(labelAnchor(0.1)).toBe("start");
  });
});

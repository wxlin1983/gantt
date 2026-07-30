import { describe, expect, it } from "vitest";

import {
  BAR_HEIGHT,
  BASELINE_HEIGHT,
  ROW_HEIGHT,
  SUMMARY_HEIGHT,
  axisTiers,
  baselineY,
  barY,
  elbowPath,
  groupSpans,
  paddedRange,
  pickScale,
  rowCentre,
  spanWidth,
  summaryY,
  ticks,
  timeToX,
  visibleRows,
  xToTime,
  type Viewport,
} from "./ganttLayout";

const viewport: Viewport = {
  from: new Date("2026-08-14T00:00:00Z"),
  to: new Date("2026-08-16T00:00:00Z"),
  width: 960,
};

describe("timeToX", () => {
  it("maps the range onto the width", () => {
    expect(timeToX(viewport, viewport.from)).toBe(0);
    expect(timeToX(viewport, viewport.to)).toBe(960);
    expect(timeToX(viewport, new Date("2026-08-15T00:00:00Z"))).toBe(480);
  });

  it("accepts ISO strings, which is what the API returns", () => {
    expect(timeToX(viewport, "2026-08-15T00:00:00Z")).toBe(480);
  });

  it("extrapolates outside the window rather than clamping", () => {
    // Clamping would pile off-screen bars up against the edge and make them
    // look like they start at the window boundary.
    expect(timeToX(viewport, "2026-08-13T00:00:00Z")).toBeLessThan(0);
    expect(timeToX(viewport, "2026-08-17T00:00:00Z")).toBeGreaterThan(960);
  });

  it("round-trips through xToTime", () => {
    const moment = new Date("2026-08-15T06:30:00Z");
    const back = xToTime(viewport, timeToX(viewport, moment));
    expect(Math.abs(back.getTime() - moment.getTime())).toBeLessThan(1000);
  });

  it("survives a degenerate range", () => {
    const flat = { from: viewport.from, to: viewport.from, width: 960 };
    expect(timeToX(flat, viewport.from)).toBe(0);
  });
});

describe("spanWidth", () => {
  it("measures a duration in pixels", () => {
    expect(
      spanWidth(viewport, "2026-08-14T00:00:00Z", "2026-08-15T00:00:00Z"),
    ).toBe(480);
  });

  it("keeps a zero-length task visible", () => {
    // A milestone must not disappear.
    expect(
      spanWidth(viewport, "2026-08-14T06:00:00Z", "2026-08-14T06:00:00Z"),
    ).toBeGreaterThan(0);
  });
});

describe("pickScale", () => {
  it("reads short cases in hours", () => {
    expect(
      pickScale(new Date("2026-08-14"), new Date("2026-08-16")),
    ).toBe("hour");
  });

  it("reads a fortnight in days", () => {
    expect(
      pickScale(new Date("2026-08-01"), new Date("2026-08-14")),
    ).toBe("day");
  });

  it("reads a quarter in weeks", () => {
    expect(
      pickScale(new Date("2026-08-01"), new Date("2026-11-01")),
    ).toBe("week");
  });
});

describe("ticks", () => {
  it("widens the step so labels cannot collide", () => {
    const narrow = ticks({ ...viewport, width: 200 }, "hour");
    const wide = ticks({ ...viewport, width: 4000 }, "hour");
    // A cramped axis must render fewer labels, not overlapping ones.
    expect(narrow.length).toBeLessThan(wide.length);
  });

  it("marks midnight as major on an hour axis", () => {
    const result = ticks(viewport, "hour");
    const midnights = result.filter((tick) => tick.major);
    expect(midnights.length).toBeGreaterThan(0);
    expect(midnights.every((tick) => tick.at.getHours() === 0)).toBe(true);
  });

  it("steps a week at a time on a week axis", () => {
    const wide = {
      from: new Date("2026-08-01T00:00:00Z"),
      to: new Date("2026-10-01T00:00:00Z"),
      width: 960,
    };
    const result = ticks(wide, "week");
    expect(result.length).toBeGreaterThan(5);
    const gap = result[1]!.at.getTime() - result[0]!.at.getTime();
    expect(gap).toBe(7 * 86_400_000);
  });

  it("places every tick inside the viewport width", () => {
    for (const tick of ticks(viewport, "hour")) {
      expect(tick.x).toBeGreaterThanOrEqual(-1);
      expect(tick.x).toBeLessThanOrEqual(961);
    }
  });
});

describe("paddedRange", () => {
  it("pads so bars do not touch the edges", () => {
    const { from, to } = paddedRange([
      "2026-08-14T00:00:00Z",
      "2026-08-20T00:00:00Z",
    ]);
    expect(from.getTime()).toBeLessThan(
      new Date("2026-08-14T00:00:00Z").getTime(),
    );
    expect(to.getTime()).toBeGreaterThan(
      new Date("2026-08-20T00:00:00Z").getTime(),
    );
  });

  it("enforces a minimum span for a single short task", () => {
    // Otherwise a ten-minute task is scaled to fill the week.
    const { from, to } = paddedRange([
      "2026-08-14T09:00:00Z",
      "2026-08-14T09:10:00Z",
    ]);
    expect(to.getTime() - from.getTime()).toBeGreaterThan(11 * 3_600_000);
  });

  it("ignores nulls, which unplanned tasks produce", () => {
    const { from, to } = paddedRange([
      null,
      undefined,
      "2026-08-14T09:00:00Z",
    ]);
    expect(Number.isFinite(from.getTime())).toBe(true);
    expect(to.getTime()).toBeGreaterThan(from.getTime());
  });

  it("falls back to a window around now when given nothing", () => {
    const { from, to } = paddedRange([]);
    expect(to.getTime()).toBeGreaterThan(from.getTime());
  });
});

describe("row geometry", () => {
  it("keeps the forecast bar and its baseline ghost apart", () => {
    // The baseline sits under the forecast, not beside it: it is a reference,
    // not a competing reading.
    expect(barY(0) + BAR_HEIGHT).toBeLessThanOrEqual(baselineY(0));
  });

  it("keeps everything inside the row", () => {
    expect(barY(0)).toBeGreaterThanOrEqual(0);
    expect(baselineY(0) + BASELINE_HEIGHT).toBeLessThanOrEqual(ROW_HEIGHT);
    expect(summaryY(0)).toBeGreaterThanOrEqual(0);
    expect(summaryY(0) + SUMMARY_HEIGHT).toBeLessThanOrEqual(ROW_HEIGHT);
  });

  it("makes a summary bar heavier than the tasks under it", () => {
    expect(SUMMARY_HEIGHT).toBeGreaterThan(BAR_HEIGHT);
  });

  it("centres both bars on the same row line", () => {
    expect(barY(0) + BAR_HEIGHT / 2).toBeCloseTo(rowCentre(0));
    expect(summaryY(0) + SUMMARY_HEIGHT / 2).toBeCloseTo(rowCentre(0));
  });

  it("offsets each row by a full row height", () => {
    expect(barY(3) - barY(2)).toBe(ROW_HEIGHT);
  });
});

describe("axisTiers", () => {
  function span(days: number): Viewport {
    const from = new Date("2026-08-01T00:00:00Z");
    return {
      from,
      to: new Date(from.getTime() + days * 86_400_000),
      width: 960,
    };
  }

  it("reads a quarter as months over weeks", () => {
    const tiers = axisTiers(span(90));
    expect(tiers.major.length).toBeGreaterThan(2);
    expect(tiers.major[0]!.label).toMatch(/[A-Za-z]/);
    expect(tiers.minor[0]!.label).toMatch(/^W\d+$/);
  });

  it("reads a fortnight as weeks over days", () => {
    const tiers = axisTiers(span(14));
    expect(tiers.major[0]!.label).toMatch(/^W\d+$/);
    expect(tiers.minor[0]!.label).toMatch(/^\d+\/\d+$/);
  });

  it("reads a couple of days as days over hours", () => {
    const tiers = axisTiers(span(2));
    expect(tiers.minor[0]!.label).toMatch(/^\d{2}:00$/);
  });

  it("always gives both tiers something to show", () => {
    for (const days of [1, 5, 20, 60, 200]) {
      const tiers = axisTiers(span(days));
      expect(tiers.major.length).toBeGreaterThan(0);
      expect(tiers.minor.length).toBeGreaterThan(0);
    }
  });

  it("restarts week numbering each month", () => {
    const tiers = axisTiers(span(90));
    const labels = tiers.minor.map((tick) => tick.label);
    // W1 appears once per month rather than counting up all quarter
    expect(labels.filter((label) => label === "W1").length).toBeGreaterThan(1);
  });
});

describe("elbowPath", () => {
  it("is orthogonal, not curved", () => {
    const d = elbowPath({ x: 10, y: 10 }, { x: 200, y: 40 });
    // A grid deserves right angles; C would mean a bezier
    expect(d).not.toContain("C");
    expect(d).toContain("H");
    expect(d).toContain("V");
  });

  it("starts and ends where it was asked to", () => {
    const d = elbowPath({ x: 10, y: 10 }, { x: 200, y: 40 });
    expect(d.startsWith("M 10 10")).toBe(true);
    expect(d.trimEnd().endsWith("H 200")).toBe(true);
  });

  it("collapses to a straight run on the same row", () => {
    expect(elbowPath({ x: 10, y: 20 }, { x: 90, y: 20 })).toBe("M 10 20 H 90");
  });

  it("doubles back when the successor starts first", () => {
    // Otherwise the connector would cut straight through both bars
    const d = elbowPath({ x: 200, y: 10 }, { x: 40, y: 40 });
    expect(d).toContain("H 40");
    expect(d.split("V").length).toBeGreaterThan(2);
  });

  it("turns downwards and upwards alike", () => {
    expect(elbowPath({ x: 0, y: 100 }, { x: 200, y: 10 })).toContain("V");
  });
});

describe("groupSpans", () => {
  const task = (phase: string, from: string, to: string) => ({
    phase,
    forecast_start: from,
    forecast_end: to,
  });

  it("spans each phase from its first start to its last end", () => {
    const spans = groupSpans([
      task("Prep", "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z"),
      task("Prep", "2026-08-03T00:00:00Z", "2026-08-06T00:00:00Z"),
      task("Test", "2026-08-07T00:00:00Z", "2026-08-09T00:00:00Z"),
    ]);
    expect(spans.map((span) => span.key)).toEqual(["Prep", "Test"]);
    expect(spans[0]!.from.toISOString()).toBe("2026-08-01T00:00:00.000Z");
    expect(spans[0]!.to.toISOString()).toBe("2026-08-06T00:00:00.000Z");
  });

  it("keeps the order the tasks arrived in", () => {
    const spans = groupSpans([
      task("Z", "2026-08-05T00:00:00Z", "2026-08-06T00:00:00Z"),
      task("A", "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z"),
    ]);
    // Phase order comes from the template, not the alphabet
    expect(spans.map((span) => span.key)).toEqual(["Z", "A"]);
  });

  it("gives each phase its own palette slot", () => {
    const spans = groupSpans([
      task("A", "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z"),
      task("B", "2026-08-02T00:00:00Z", "2026-08-03T00:00:00Z"),
    ]);
    expect(new Set(spans.map((span) => span.colour)).size).toBe(2);
  });

  it("handles ungrouped tasks as one span", () => {
    const spans = groupSpans([
      task("", "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z"),
    ]);
    expect(spans).toHaveLength(1);
    expect(spans[0]!.label).toBe("Tasks");
  });

  it("drops a phase whose tasks have no forecast at all", () => {
    const spans = groupSpans([
      { phase: "Ghost", forecast_start: null, forecast_end: null },
    ]);
    expect(spans).toEqual([]);
  });
});

describe("visibleRows", () => {
  it("windows the rows so long cases stay cheap", () => {
    const { first, last } = visibleRows(0, 400, 500);
    expect(first).toBe(0);
    expect(last).toBeLessThan(500);
  });

  it("overscans above and below the fold", () => {
    const { first, last } = visibleRows(ROW_HEIGHT * 50, 400, 500);
    expect(first).toBeLessThan(50);
    expect(last).toBeGreaterThan(50 + 400 / ROW_HEIGHT);
  });

  it("never runs past the end", () => {
    const { last } = visibleRows(99_999, 400, 12);
    expect(last).toBeLessThanOrEqual(12);
  });
});

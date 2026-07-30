import { describe, expect, it } from "vitest";

import {
  BAR_HEIGHT,
  ROW_HEIGHT,
  baselineY,
  dependencyPath,
  forecastY,
  paddedRange,
  pickScale,
  spanWidth,
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
  it("stacks baseline above forecast without overlap", () => {
    const upper = baselineY(0);
    const lower = forecastY(0);
    expect(upper + BAR_HEIGHT).toBeLessThanOrEqual(lower);
  });

  it("keeps both tracks inside the row", () => {
    expect(baselineY(0)).toBeGreaterThanOrEqual(0);
    expect(forecastY(0) + BAR_HEIGHT).toBeLessThanOrEqual(ROW_HEIGHT);
  });

  it("offsets each row by a full row height", () => {
    expect(baselineY(3) - baselineY(2)).toBe(ROW_HEIGHT);
  });
});

describe("dependencyPath", () => {
  it("connects the predecessor's end to the successor's start", () => {
    const path = dependencyPath(
      viewport,
      "2026-08-14T12:00:00Z",
      0,
      "2026-08-15T00:00:00Z",
      1,
      0,
      "",
    );
    expect(path.from.x).toBe(timeToX(viewport, "2026-08-14T12:00:00Z"));
    expect(path.to.x).toBe(timeToX(viewport, "2026-08-15T00:00:00Z"));
    expect(path.d.startsWith("M ")).toBe(true);
    expect(path.lag).toBeUndefined();
  });

  it("reports a lag segment separately from the connector", () => {
    // Lag is waiting, not work: drawing it as one continuous bar would read as
    // somebody idling on the job.
    const path = dependencyPath(
      viewport,
      "2026-08-14T12:00:00Z",
      0,
      "2026-08-14T16:00:00Z",
      1,
      4 * 3600,
      "4H",
    );
    expect(path.lag).toBeDefined();
    expect(path.lag!.label).toBe("4H");
    expect(path.lag!.x2).toBeGreaterThan(path.lag!.x1);
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

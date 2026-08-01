import { describe, expect, it } from "vitest";

import {
  formatDuration,
  formatSpan,
  isInstant,
  ownerLabel,
  variance,
} from "./format";

describe("formatSpan", () => {
  it("reads a measured difference in units people use", () => {
    // The bug it exists for: `formatDuration` speaks the DSL's units, so a
    // variance of twelve and a bit days came out as "1131452S"
    expect(formatDuration(1_131_452)).toBe("1131452S");
    expect(formatSpan(1_131_452)).toBe("13d 2h");
  });

  it("drops the smaller unit when it is zero", () => {
    expect(formatSpan(86_400)).toBe("1d");
    expect(formatSpan(7_200)).toBe("2h");
  });

  it("keeps short spans short", () => {
    expect(formatSpan(45)).toBe("45s");
    expect(formatSpan(600)).toBe("10m");
    expect(formatSpan(23_400)).toBe("6h 30m");
  });

  it("ignores the sign, which the caller supplies", () => {
    expect(formatSpan(-7_200)).toBe(formatSpan(7_200));
  });
});

describe("isInstant", () => {
  it("spots a task ticked off without ever being started", () => {
    // Completion records only an end, so the forecast collapses to a point;
    // drawing that as a range would assert a working window nobody reported
    expect(
      isInstant({
        forecast_start: "2026-07-30T15:32:18Z",
        forecast_end: "2026-07-30T15:32:18Z",
      }),
    ).toBe(true);
  });

  it("leaves a task that really occupied time alone", () => {
    expect(
      isInstant({
        forecast_start: "2026-07-30T03:32:18Z",
        forecast_end: "2026-07-30T15:32:18Z",
      }),
    ).toBe(false);
  });

  it("is not fooled by a missing forecast", () => {
    expect(isInstant({ forecast_start: null, forecast_end: null })).toBe(
      false,
    );
  });
});

describe("variance", () => {
  it("reports nothing for a task with no plan to miss", () => {
    expect(
      variance({ baseline_end: null, forecast_end: "2026-08-01T00:00:00Z" }),
    ).toBeNull();
  });

  it("is positive when the forecast runs past the plan", () => {
    expect(
      variance({
        baseline_end: "2026-08-01T00:00:00Z",
        forecast_end: "2026-08-01T04:00:00Z",
      }),
    ).toBe(14_400);
  });
});

describe("ownerLabel", () => {
  it("names the person when there is one", () => {
    expect(
      ownerLabel({ owner_id: 3, owner_source: "role:pm" }, { "3": "Alice" }),
    ).toBe("Alice");
  });

  it("says which role is still waiting when nobody resolved", () => {
    // A blank hides the reason; the role name is the actionable part
    expect(ownerLabel({ owner_id: null, owner_source: "role:pm" }, {})).toBe(
      "unassigned · pm",
    );
    expect(
      ownerLabel({ owner_id: null, owner_source: "group_lead:qa" }, {}),
    ).toBe("unassigned · qa lead");
  });

  it("falls back to plain unassigned when the source says nothing", () => {
    expect(ownerLabel({ owner_id: null, owner_source: "literal" }, {})).toBe(
      "unassigned",
    );
  });

  it("shows the id when the name map is missing an entry", () => {
    expect(ownerLabel({ owner_id: 7, owner_source: "manual" }, {})).toBe("#7");
  });
});

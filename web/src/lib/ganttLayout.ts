/**
 * Gantt layout arithmetic (implement.md §9.2).
 *
 * Kept as pure functions so the geometry can be tested without a DOM. Every
 * component draws from `timeToX` / `xToTime`, which means zooming only changes
 * the scale parameters rather than rebuilding anything.
 */

export type Scale = "hour" | "day" | "week";

export interface Viewport {
  /** Left edge of the visible window. */
  from: Date;
  /** Right edge of the visible window. */
  to: Date;
  /** Pixel width available for the time axis, excluding the task column. */
  width: number;
}

export interface Tick {
  at: Date;
  x: number;
  label: string;
  major: boolean;
}

const MS = { minute: 60_000, hour: 3_600_000, day: 86_400_000 } as const;

export const ROW_HEIGHT = 30;
/** The thick bar that spans a whole phase. */
export const SUMMARY_HEIGHT = 14;
/** An individual task's bar, deliberately slimmer than its summary. */
export const BAR_HEIGHT = 9;
/**
 * The baseline is a thin ghost under the forecast rather than a second bar of
 * equal weight. It is a reference, not a competing reading, and giving it the
 * same visual weight made every row look like two unrelated things.
 */
export const BASELINE_HEIGHT = 3;
export const BASELINE_GAP = 3;

export function timeToX(viewport: Viewport, at: Date | string): number {
  const moment = typeof at === "string" ? new Date(at) : at;
  const span = viewport.to.getTime() - viewport.from.getTime();
  if (span <= 0) return 0;
  const offset = moment.getTime() - viewport.from.getTime();
  return (offset / span) * viewport.width;
}

export function xToTime(viewport: Viewport, x: number): Date {
  const span = viewport.to.getTime() - viewport.from.getTime();
  return new Date(viewport.from.getTime() + (x / viewport.width) * span);
}

export function spanWidth(
  viewport: Viewport,
  from: Date | string,
  to: Date | string,
): number {
  // A zero-length task (a milestone, or one that finished instantly) still
  // needs to be visible, so widths are floored rather than allowed to vanish.
  return Math.max(timeToX(viewport, to) - timeToX(viewport, from), 2);
}

/**
 * Choose an initial scale from the total span.
 *
 * Mirrors design.md §4.5: under three days reads in hours, under three weeks
 * in days, anything longer in weeks.
 */
export function pickScale(from: Date, to: Date): Scale {
  const days = (to.getTime() - from.getTime()) / MS.day;
  if (days <= 3) return "hour";
  if (days <= 21) return "day";
  return "week";
}

const HOUR_STEPS = [1, 2, 3, 6, 12];

/**
 * Tick marks for the time axis.
 *
 * The step widens until labels stop colliding, so an axis never renders an
 * unreadable smear of overlapping text at any zoom level.
 */
export function ticks(viewport: Viewport, scale: Scale): Tick[] {
  const minSpacing = 64;
  const result: Tick[] = [];

  if (scale === "hour") {
    const step =
      HOUR_STEPS.find((hours) => {
        const pixels =
          (hours * MS.hour) /
          ((viewport.to.getTime() - viewport.from.getTime()) /
            viewport.width);
        return pixels >= minSpacing;
      }) ?? 24;
    const cursor = new Date(viewport.from);
    cursor.setMinutes(0, 0, 0);
    while (cursor.getHours() % step !== 0) {
      cursor.setHours(cursor.getHours() + 1);
    }
    while (cursor <= viewport.to) {
      result.push({
        at: new Date(cursor),
        x: timeToX(viewport, cursor),
        label: cursor.getHours() === 0 ? formatDay(cursor) : formatHour(cursor),
        major: cursor.getHours() === 0,
      });
      cursor.setHours(cursor.getHours() + step);
    }
    return result;
  }

  const stepDays = scale === "day" ? 1 : 7;
  const cursor = new Date(viewport.from);
  cursor.setHours(0, 0, 0, 0);
  while (cursor <= viewport.to) {
    result.push({
      at: new Date(cursor),
      x: timeToX(viewport, cursor),
      label: formatDay(cursor),
      major: scale === "day" ? cursor.getDay() === 1 : true,
    });
    cursor.setDate(cursor.getDate() + stepDays);
  }
  return result;
}

function formatHour(at: Date): string {
  return `${String(at.getHours()).padStart(2, "0")}:00`;
}

function formatDay(at: Date): string {
  return `${at.getMonth() + 1}/${at.getDate()}`;
}

/**
 * Pad a time range so bars do not touch the chart edges.
 *
 * Also enforces a minimum span: a single short task would otherwise be scaled
 * to fill the whole width, which reads as though it takes all week.
 */
export function paddedRange(
  moments: (Date | string | null | undefined)[],
  minimumHours = 12,
): { from: Date; to: Date } {
  const times = moments
    .filter((value): value is Date | string => Boolean(value))
    .map((value) => new Date(value).getTime())
    .filter((value) => Number.isFinite(value));

  const now = Date.now();
  if (times.length === 0) {
    return { from: new Date(now - MS.day), to: new Date(now + MS.day) };
  }

  let min = Math.min(...times);
  let max = Math.max(...times);
  const minimum = minimumHours * MS.hour;
  if (max - min < minimum) {
    const centre = (min + max) / 2;
    min = centre - minimum / 2;
    max = centre + minimum / 2;
  }
  const padding = (max - min) * 0.05;
  return { from: new Date(min - padding), to: new Date(max + padding) };
}

export function rowCentre(row: number): number {
  return row * ROW_HEIGHT + ROW_HEIGHT / 2;
}

/** Top of a task's forecast bar. */
export function barY(row: number): number {
  return rowCentre(row) - BAR_HEIGHT / 2;
}

/** Top of the thicker bar that spans a whole phase. */
export function summaryY(row: number): number {
  return rowCentre(row) - SUMMARY_HEIGHT / 2;
}

/**
 * Top of the baseline ghost, tucked under the forecast bar.
 *
 * Below rather than above: the eye lands on the live value first and treats
 * the plan as the reference it is measured against.
 */
export function baselineY(row: number): number {
  return rowCentre(row) + BAR_HEIGHT / 2 + BASELINE_GAP;
}

/**
 * Which rows are visible, so long cases do not render hundreds of hidden rows.
 */
export function visibleRows(
  scrollTop: number,
  viewportHeight: number,
  total: number,
  overscan = 10,
): { first: number; last: number } {
  const first = Math.max(Math.floor(scrollTop / ROW_HEIGHT) - overscan, 0);
  const last = Math.min(
    Math.ceil((scrollTop + viewportHeight) / ROW_HEIGHT) + overscan,
    total,
  );
  return { first, last };
}


/* --- two-tier axis -------------------------------------------------------- */

export interface AxisTiers {
  /** Wide spans, labelled and separated by a full-height rule. */
  major: Tick[];
  /** Subdivisions inside each major span. */
  minor: Tick[];
}

/**
 * A month-over-week axis, or its equivalent at other zoom levels.
 *
 * One row of ticks has to serve two questions at once -- "roughly when" and
 * "exactly when" -- and a single row can only answer one of them legibly.
 */
export function axisTiers(viewport: Viewport): AxisTiers {
  const days =
    (viewport.to.getTime() - viewport.from.getTime()) / MS.day;

  if (days > 45) {
    return {
      major: months(viewport),
      minor: thin(weeks(viewport, false), viewport),
    };
  }
  if (days > 8) {
    return {
      major: weeks(viewport, true),
      minor: thin(days_(viewport), viewport),
    };
  }
  return { major: days_(viewport), minor: thin(hours(viewport, 3), viewport) };
}

/** Labels need room. Below it they overlap into an unreadable smear. */
const MIN_LABEL_SPACING = 46;

/**
 * Drop every other tick until the labels fit.
 *
 * Generating at a fixed step and hoping meant a week-long case rendered
 * four-hourly ticks 30px apart, which ran together into a grey band. Halving
 * a regular series keeps it regular, which a "pick the next unit up" rule
 * does not.
 */
function thin(series: Tick[], viewport: Viewport): Tick[] {
  let result = series;
  while (
    result.length > 2 &&
    viewport.width / (result.length - 1) < MIN_LABEL_SPACING
  ) {
    result = result.filter((_, index) => index % 2 === 0);
  }
  return result;
}

function months(viewport: Viewport): Tick[] {
  const out: Tick[] = [];
  const cursor = new Date(viewport.from);
  cursor.setDate(1);
  cursor.setHours(0, 0, 0, 0);
  while (cursor <= viewport.to) {
    out.push({
      at: new Date(cursor),
      x: timeToX(viewport, cursor),
      label: cursor.toLocaleDateString(undefined, { month: "long" }),
      major: true,
    });
    cursor.setMonth(cursor.getMonth() + 1);
  }
  return out;
}

/**
 * Weekly ticks, labelled by week of their own month.
 *
 * The number comes from the date rather than from a counter walking the view:
 * counting restarted at 1 for whichever week the viewport happened to open on,
 * so a fortnight straddling a month boundary was labelled "W1 W1 W2". When
 * weeks are the major tier they carry the month too, since "W1" alone cannot
 * say which month it belongs to.
 */
function weeks(viewport: Viewport, withMonth: boolean): Tick[] {
  const out: Tick[] = [];
  const cursor = new Date(viewport.from);
  cursor.setHours(0, 0, 0, 0);
  // Anchor on Monday so week numbering does not drift with the view.
  cursor.setDate(cursor.getDate() - ((cursor.getDay() + 6) % 7));
  while (cursor <= viewport.to) {
    const index = Math.floor((cursor.getDate() - 1) / 7) + 1;
    const month = cursor.toLocaleDateString(undefined, { month: "short" });
    out.push({
      at: new Date(cursor),
      x: timeToX(viewport, cursor),
      label: withMonth ? `${month} W${index}` : `W${index}`,
      major: withMonth,
    });
    cursor.setDate(cursor.getDate() + 7);
  }
  return out;
}

function days_(viewport: Viewport): Tick[] {
  const out: Tick[] = [];
  const cursor = new Date(viewport.from);
  cursor.setHours(0, 0, 0, 0);
  while (cursor <= viewport.to) {
    out.push({
      at: new Date(cursor),
      x: timeToX(viewport, cursor),
      label: `${cursor.getMonth() + 1}/${cursor.getDate()}`,
      major: cursor.getDay() === 1,
    });
    cursor.setDate(cursor.getDate() + 1);
  }
  return out;
}

function hours(viewport: Viewport, step: number): Tick[] {
  const out: Tick[] = [];
  const cursor = new Date(viewport.from);
  cursor.setMinutes(0, 0, 0);
  while (cursor.getHours() % step !== 0) {
    cursor.setHours(cursor.getHours() + 1);
  }
  while (cursor <= viewport.to) {
    out.push({
      at: new Date(cursor),
      x: timeToX(viewport, cursor),
      label: `${String(cursor.getHours()).padStart(2, "0")}:00`,
      major: cursor.getHours() === 0,
    });
    cursor.setHours(cursor.getHours() + step);
  }
  return out;
}

/* --- connectors ----------------------------------------------------------- */

/**
 * Right-angled connector with rounded corners.
 *
 * Orthogonal rather than curved: a Gantt is a grid, and elbows read as "this
 * then that" where a bezier reads as decoration. It also stays legible when
 * several edges converge on one bar, which is exactly when a curve turns into
 * a knot.
 */
export function elbowPath(
  from: { x: number; y: number },
  to: { x: number; y: number },
  radius = 5,
): string {
  const stub = 10;
  const dy = to.y - from.y;

  if (Math.abs(dy) < 1) return `M ${from.x} ${from.y} H ${to.x}`;

  // Long horizontal runs happen in the gutter between rows, never along a
  // row's centre line. On its own row a connector spanning most of the chart
  // is indistinguishable from a bar -- which is exactly what it looked like
  // for a task completed far from its neighbours, where every edge touching
  // it crosses the whole width. Crossing is left to the verticals, which
  // nobody mistakes for a bar.
  const channel = from.y + (dy > 0 ? ROW_HEIGHT / 2 : -ROW_HEIGHT / 2);

  return orthogonalPath(
    [
      from,
      { x: from.x + stub, y: from.y },
      { x: from.x + stub, y: channel },
      { x: to.x - stub, y: channel },
      { x: to.x - stub, y: to.y },
      to,
    ],
    radius,
  );
}

/**
 * An orthogonal polyline with rounded corners.
 *
 * Waypoints must alternate horizontal and vertical; degenerate segments are
 * dropped, so a caller can emit the general six-point route and let the
 * straight cases collapse themselves rather than special-casing each shape.
 */
export function orthogonalPath(
  points: { x: number; y: number }[],
  radius = 5,
): string {
  const via = points.filter(
    (point, index) =>
      index === 0 ||
      Math.abs(point.x - points[index - 1]!.x) > 0.5 ||
      Math.abs(point.y - points[index - 1]!.y) > 0.5,
  );
  if (via.length < 2) return "";

  const parts = [`M ${round(via[0]!.x)} ${round(via[0]!.y)}`];
  for (let index = 1; index < via.length; index += 1) {
    const previous = via[index - 1]!;
    const current = via[index]!;
    const next = via[index + 1];
    if (!next) {
      parts.push(lineTo(previous, current));
      break;
    }
    // Stop short of the corner, arc through it, and resume on the next leg.
    const into = shorten(current, previous, radius);
    const outOf = shorten(current, next, radius);
    parts.push(lineTo(previous, into));
    parts.push(
      `Q ${round(current.x)} ${round(current.y)} ` +
        `${round(outOf.x)} ${round(outOf.y)}`,
    );
    via[index] = outOf;
  }
  return parts.join(" ");
}

function lineTo(from: { x: number; y: number }, to: { x: number; y: number }) {
  if (Math.abs(to.y - from.y) < 0.5) return `H ${round(to.x)}`;
  if (Math.abs(to.x - from.x) < 0.5) return `V ${round(to.y)}`;
  return `L ${round(to.x)} ${round(to.y)}`;
}

/** A point `by` pixels from `corner` along the way to `towards`. */
function shorten(
  corner: { x: number; y: number },
  towards: { x: number; y: number },
  by: number,
): { x: number; y: number } {
  const dx = towards.x - corner.x;
  const dy = towards.y - corner.y;
  const length = Math.hypot(dx, dy) || 1;
  const step = Math.min(by, length / 2);
  return {
    x: corner.x + (dx / length) * step,
    y: corner.y + (dy / length) * step,
  };
}

function round(value: number): number {
  return Math.round(value * 10) / 10;
}

/* --- grouping ------------------------------------------------------------- */

export interface GroupSpan {
  key: string;
  label: string;
  from: Date;
  to: Date;
  /** Index into the phase palette, stable per group. */
  colour: number;
}

/**
 * Span of each phase, for the thick summary bar above its tasks.
 *
 * A phase is the unit people plan in, so it deserves a bar of its own rather
 * than only a heading; the summary is what makes "are we through testing yet"
 * answerable without reading every row.
 */
export function groupSpans(
  tasks: {
    phase: string;
    forecast_start: string | null;
    forecast_end: string | null;
  }[],
): GroupSpan[] {
  const order: string[] = [];
  const buckets = new Map<string, { from: number; to: number }>();

  for (const task of tasks) {
    const key = task.phase || "";
    if (!buckets.has(key)) {
      order.push(key);
      buckets.set(key, { from: Infinity, to: -Infinity });
    }
    const bucket = buckets.get(key)!;
    if (task.forecast_start) {
      bucket.from = Math.min(bucket.from, Date.parse(task.forecast_start));
    }
    if (task.forecast_end) {
      bucket.to = Math.max(bucket.to, Date.parse(task.forecast_end));
    }
  }

  return order
    .map((key, index) => {
      const bucket = buckets.get(key)!;
      if (!Number.isFinite(bucket.from) || !Number.isFinite(bucket.to)) {
        return null;
      }
      return {
        key,
        label: key || "Tasks",
        from: new Date(bucket.from),
        to: new Date(bucket.to),
        colour: index,
      };
    })
    .filter((span): span is GroupSpan => span !== null);
}

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

export const ROW_HEIGHT = 44;
export const BAR_HEIGHT = 9;
/** Gap between the baseline bar and the forecast bar within one row. */
export const TRACK_GAP = 4;

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

export interface DependencyPath {
  from: { x: number; y: number };
  to: { x: number; y: number };
  /** SVG path for the connector. */
  d: string;
  /** Lag segment, drawn as a dashed run before the connector. */
  lag?: { x1: number; x2: number; y: number; label: string };
}

/**
 * Bezier connector between two bars, with the lag drawn separately.
 *
 * Lag has to look different from work: an unbroken bar across a four-hour wait
 * would read as somebody sitting idle on the job (design.md §4.2).
 */
export function dependencyPath(
  viewport: Viewport,
  predecessorEnd: Date | string,
  predecessorRow: number,
  successorStart: Date | string,
  successorRow: number,
  lagSeconds: number,
  lagLabel: string,
): DependencyPath {
  const startX = timeToX(viewport, predecessorEnd);
  const startY = rowCentre(predecessorRow);
  const endX = timeToX(viewport, successorStart);
  const endY = rowCentre(successorRow);

  const lagX = lagSeconds > 0 ? startX : undefined;
  const control = Math.max(Math.abs(endX - startX) * 0.4, 18);
  const d = `M ${startX} ${startY} C ${startX + control} ${startY} ${
    endX - control
  } ${endY} ${endX} ${endY}`;

  return {
    from: { x: startX, y: startY },
    to: { x: endX, y: endY },
    d,
    lag:
      lagX === undefined
        ? undefined
        : { x1: lagX, x2: endX, y: startY, label: lagLabel },
  };
}

export function rowCentre(row: number): number {
  return row * ROW_HEIGHT + ROW_HEIGHT / 2;
}

/** Y offset of the baseline (upper) track within a row. */
export function baselineY(row: number): number {
  return rowCentre(row) - BAR_HEIGHT - TRACK_GAP / 2;
}

/** Y offset of the forecast (lower) track within a row. */
export function forecastY(row: number): number {
  return rowCentre(row) + TRACK_GAP / 2;
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

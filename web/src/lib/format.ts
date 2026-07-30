/** Display helpers. */

import type { CaseHealth, TaskStatus } from "../api/types";

export function formatDuration(seconds: number): string {
  if (seconds === 0) return "0";
  if (seconds % 86400 === 0) return `${seconds / 86400}D`;
  if (seconds % 3600 === 0) return `${seconds / 3600}H`;
  if (seconds % 60 === 0) return `${seconds / 60}M`;
  return `${seconds}S`;
}

/** Signed offset, e.g. `+8H` — how the case list reports variance. */
export function formatDelta(seconds: number): string {
  if (seconds === 0) return "on time";
  const sign = seconds > 0 ? "+" : "-";
  return sign + formatDuration(Math.abs(seconds));
}

export function formatMoment(value: string | null): string {
  if (!value) return "—";
  const at = new Date(value);
  return `${at.getMonth() + 1}/${at.getDate()} ${String(
    at.getHours(),
  ).padStart(2, "0")}:${String(at.getMinutes()).padStart(2, "0")}`;
}

export function formatDate(value: string | null): string {
  if (!value) return "—";
  const at = new Date(value);
  return `${at.getFullYear()}-${String(at.getMonth() + 1).padStart(
    2,
    "0",
  )}-${String(at.getDate()).padStart(2, "0")}`;
}

export function formatPercent(ratio: number | null): string {
  if (ratio === null || ratio === undefined) return "—";
  return `${Math.round(ratio * 100)}%`;
}

/**
 * Status is conveyed by icon and text as well as colour.
 *
 * Colour alone fails for colour-blind users and in printouts, so every status
 * carries a glyph too (design.md §4.4).
 */
export const STATUS_META: Record<
  TaskStatus,
  { icon: string; label: string; tone: string }
> = {
  pending: { icon: "○", label: "Pending", tone: "neutral" },
  ready: { icon: "◉", label: "Ready", tone: "primary" },
  running: { icon: "▶", label: "Running", tone: "primary" },
  done: { icon: "✓", label: "Done", tone: "success" },
  failed: { icon: "✕", label: "Failed", tone: "danger" },
  cancelled: { icon: "⊘", label: "Cancelled", tone: "neutral" },
};

export const HEALTH_META: Record<
  CaseHealth,
  { icon: string; label: string; tone: string }
> = {
  on_track: { icon: "●", label: "On track", tone: "success" },
  at_risk: { icon: "●", label: "At risk", tone: "warning" },
  overdue: { icon: "●", label: "Overdue", tone: "danger" },
};

/** A ready task past its planned start: the one actionable warning. */
export function isLateStart(
  status: TaskStatus,
  baselineStart: string | null,
): boolean {
  if (status !== "ready" || !baselineStart) return false;
  return Date.now() > new Date(baselineStart).getTime();
}

export function variance(task: {
  baseline_end: string | null;
  forecast_end: string | null;
}): number | null {
  // An unplanned task has no baseline, so there is nothing to compare against
  // and reporting a variance would be inventing one.
  if (!task.baseline_end || !task.forecast_end) return null;
  return Math.round(
    (new Date(task.forecast_end).getTime() -
      new Date(task.baseline_end).getTime()) /
      1000,
  );
}

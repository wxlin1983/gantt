/**
 * SVG Gantt renderer (implement.md §9.2, design.md §4).
 *
 * Built rather than borrowed: the chart needs a baseline ghost under each
 * forecast, orthogonal connectors that stay legible when several converge, a
 * buffer block at the tail, and a window that can be fitted to a subset of the
 * tasks. Libraries assume one bar per row and stop there.
 *
 * Colour encodes **phase**, not status. Phase is what makes a chart readable
 * from across the room, and status is still carried by the glyph in the rail,
 * the bar's fill treatment, and its label -- never by colour alone (§4.5).
 *
 * The canvas keeps its dark palette in both app themes. It is a distinct
 * surface, like a map or an editor, and the pale-on-white version of the same
 * design loses the separation between the plot and the page.
 */

import { useEffect, useMemo, useRef, useState } from "react";

import type { CaseDetail, Task } from "../../api/types";
import {
  STATUS_META,
  formatSpan,
  isInstant,
  variance,
} from "../../lib/format";
import {
  BAR_HEIGHT,
  BAR_RADIUS,
  BASELINE_HEIGHT,
  ROW_HEIGHT,
  type Viewport,
  axisTiers,
  baselineY,
  barY,
  elbowPath,
  groupSpans,
  paddedRange,
  rowCentre,
  spanWidth,
  timeToX,
} from "../../lib/ganttLayout";

const RAIL = 210;
const AXIS = 46;
/** Phase palette; index cycles, so a sixth phase reuses the first colour. */
const PALETTE = 6;

/** 1 means the chosen range fits the window exactly, with nothing to scroll. */
const MIN_ZOOM = 1;
/** Beyond this a day is wider than most screens and panning stops helping. */
const MAX_ZOOM = 12;

interface Props {
  detail: CaseDetail;
  onSelect: (task: Task) => void;
  selectedId?: number | null;
  showCriticalPath?: boolean;
}

type Row =
  | {
      kind: "summary";
      key: string;
      label: string;
      colour: number;
      owner: string | null;
    }
  | { kind: "task"; task: Task; colour: number };

/** Which window the chart fits: everything, or only what is still to do. */
type Fit = "all" | "open";

/** Still to do. A failed task is very much not finished. */
function isOpen(task: Task): boolean {
  return task.status !== "done" && task.status !== "cancelled";
}

export function GanttChart({
  detail,
  onSelect,
  selectedId,
  showCriticalPath = true,
}: Props) {
  const [viewWidth, setViewWidth] = useState(880);
  const [hovered, setHovered] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  const [fit, setFit] = useState<Fit>("all");
  const canvas = useRef<HTMLDivElement | null>(null);

  // The plot is as wide as the window times the zoom, so zooming in makes the
  // canvas overflow and the container scroll. At zoom 1 the chosen range fits
  // exactly and there is nothing to scroll.
  const width = Math.round(viewWidth * zoom);

  // Measured continuously, not once. The axis scale is derived from this
  // width, so a chart sized at mount kept its first guess through every
  // window resize and every change to what sits beside it.
  useEffect(() => {
    const node = canvas.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      const measured = Math.round(entries[0]!.contentRect.width);
      if (measured > 0) setViewWidth(measured);
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const groups = useMemo(() => groupSpans(detail.tasks), [detail.tasks]);
  const colourOf = useMemo(() => {
    const map = new Map<string, number>();
    groups.forEach((group, index) => map.set(group.key, index % PALETTE));
    return map;
  }, [groups]);

  const rows: Row[] = useMemo(() => {
    const out: Row[] = [];
    let current: string | null = null;
    for (const task of detail.tasks) {
      const key = task.phase || "";
      const colour = colourOf.get(key) ?? 0;
      if (key !== current) {
        const span = groups.find((group) => group.key === key);
        if (span) {
          out.push({
            kind: "summary",
            key,
            label: span.label,
            colour,
            owner: ownerLabel(detail, key),
          });
        }
        current = key;
      }
      out.push({ kind: "task", task, colour });
    }
    return out;
  }, [detail, groups, colourOf]);

  const rowOf = useMemo(() => {
    const map = new Map<string, number>();
    rows.forEach((row, index) => {
      if (row.kind === "task") map.set(row.task.name, index);
    });
    return map;
  }, [rows]);

  const viewport: Viewport = useMemo(() => {
    // Framed on the forecast: what is going to happen. The baseline is
    // as-late-as-possible, so on any case with slack it sits bunched against
    // the target -- including it stretched the frame to the deadline and
    // squeezed a week of real work into a fifth of the width, with the
    // baselines left as detached grey stubs a fortnight away. The target and
    // today still show, pinned to whichever edge they fall off.
    // `open` frames only what is still to come, which on a long-running case
    // is the part you are actually working on; finished tasks keep their bars,
    // they just stop dictating how wide the window has to be.
    const framed =
      fit === "open" && detail.tasks.some(isOpen)
        ? detail.tasks.filter(isOpen)
        : detail.tasks;
    const forecast = framed.flatMap((task) => [
      task.forecast_start,
      task.forecast_end,
    ]);
    const anything = forecast.some(Boolean)
      ? forecast
      : // A case whose tasks have no forecast at all has only its plan.
        [
          ...detail.tasks.flatMap((task) => [
            task.baseline_start,
            task.baseline_end,
          ]),
          detail.target_date,
        ];
    const planned = anything;
    const work = paddedRange(planned);
    // `now` earns a place in the frame only when it is near the work. A case
    // planned to start in two months was otherwise drawn as two months of
    // empty grid with every bar crushed into the last few pixels -- truthful,
    // and useless as a chart. When it falls outside, the marker is pinned to
    // the edge instead of vanishing.
    const now = Date.now();
    const span = work.to.getTime() - work.from.getTime();
    const near =
      now >= work.from.getTime() - span * 0.25 &&
      now <= work.to.getTime() + span * 0.25;
    const { from, to } = near
      ? paddedRange([...planned, new Date(now).toISOString()])
      : work;
    return { from, to, width };
  }, [detail.tasks, detail.target_date, width, fit]);

  const tiers = useMemo(() => axisTiers(viewport), [viewport]);
  const height = rows.length * ROW_HEIGHT;
  const connected = useMemo(
    () => relatedTasks(detail, hovered),
    [detail, hovered],
  );

  const planDeadline = new Date(
    new Date(detail.target_date).getTime() - detail.buffer_seconds * 1000,
  );

  const openCount = detail.tasks.filter(isOpen).length;

  /** Zoom about the middle of what is on screen, so the view does not jump. */
  function rezoom(next: number) {
    const clamped = Math.min(Math.max(next, MIN_ZOOM), MAX_ZOOM);
    if (clamped === zoom) return;
    const node = canvas.current;
    const anchor = node
      ? (node.scrollLeft + node.clientWidth / 2) / Math.max(width, 1)
      : 0.5;
    setZoom(clamped);
    if (node) {
      requestAnimationFrame(() => {
        node.scrollLeft =
          anchor * viewWidth * clamped - node.clientWidth / 2;
      });
    }
  }

  return (
    <div className="gantt-frame">
      <div className="gantt-controls">
        <div className="segmented">
          <button
            type="button"
            className={fit === "all" ? "active" : ""}
            onClick={() => setFit("all")}
          >
            All steps
          </button>
          <button
            type="button"
            className={fit === "open" ? "active" : ""}
            onClick={() => setFit("open")}
            disabled={openCount === 0}
            title={
              openCount === 0
                ? "Everything is finished"
                : "Fit the window to what is still to do"
            }
          >
            Unfinished{openCount > 0 ? ` (${openCount})` : ""}
          </button>
        </div>
        <span className="spacer" />
        <div className="segmented">
          <button
            type="button"
            onClick={() => rezoom(zoom / 1.6)}
            disabled={zoom <= MIN_ZOOM}
            aria-label="Zoom out"
          >
            −
          </button>
          <button
            type="button"
            onClick={() => rezoom(zoom * 1.6)}
            disabled={zoom >= MAX_ZOOM}
            aria-label="Zoom in"
          >
            +
          </button>
        </div>
        {zoom > MIN_ZOOM && (
          <button
            type="button"
            className="button small"
            onClick={() => rezoom(MIN_ZOOM)}
          >
            Fit
          </button>
        )}
      </div>

      <div className="gantt" style={{ ["--rail" as string]: `${RAIL}px` }}>
          <div className="gantt-rail">
          <div className="gantt-rail-head" style={{ height: AXIS }}>
            {detail.name}
          </div>
          {rows.map((row) =>
            row.kind === "summary" ? (
              <div
                key={`g-${row.key}`}
                className="gantt-group"
                style={{ height: ROW_HEIGHT }}
              >
                <span>{row.label}</span>
                {/* The owner used to sit above the phase bar. With the bar gone
                    it belongs here, beside the name it qualifies. */}
                {row.owner && (
                  <span className="gantt-group-owner">{row.owner}</span>
                )}
              </div>
            ) : (
              <button
                key={row.task.id}
                type="button"
                className={[
                  "gantt-row",
                  row.task.id === selectedId ? "selected" : "",
                  hovered && !connected.has(row.task.name) ? "dimmed" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                style={{ height: ROW_HEIGHT }}
                onClick={() => onSelect(row.task)}
                onMouseEnter={() => setHovered(row.task.name)}
                onMouseLeave={() => setHovered(null)}
                title={`${STATUS_META[row.task.status].label}${
                  row.task.is_optional ? " · optional" : ""
                }${row.task.is_unplanned ? " · added after creation" : ""}`}
              >
                <span className="gantt-row-name">
                  {row.task.display_name || row.task.name}
                </span>
                <span className={`status status-${row.task.status}`}>
                  {STATUS_META[row.task.status].icon}
                </span>
              </button>
            ),
          )}
        </div>

        <div className="gantt-canvas" ref={canvas}>
          <svg
            width={width}
            height={height + AXIS}
            role="img"
            aria-label={`Schedule for ${detail.name}`}
          >
            {/* Major columns run the full height, which is what makes a bar's
                position readable without tracing back up to the axis. */}
            <g className="gantt-grid">
              {tiers.major.map((tick) => (
                <line
                  key={`M${tick.at.toISOString()}`}
                  x1={tick.x}
                  x2={tick.x}
                  y1={0}
                  y2={height + AXIS}
                  className="grid-major"
                />
              ))}
              {tiers.minor.map((tick) => (
                <line
                  key={`m${tick.at.toISOString()}`}
                  x1={tick.x}
                  x2={tick.x}
                  y1={AXIS}
                  y2={height + AXIS}
                  className="grid-minor"
                />
              ))}
            </g>

            <g className="gantt-axis">
              {tiers.major.map((tick, index) => {
                const next = tiers.major[index + 1];
                const right = Math.min(next ? next.x : width, width);
                const left = Math.max(tick.x, 0);
                // A span clipped by the chart edge has nowhere to put its name;
                // half a word ("Octobe") is worse than none. Wide enough spans
                // keep their centre inside the plot so the text cannot overrun.
                if (right - left < 44) return null;
                const pad = 34;
                return (
                  <text
                    key={`ML${tick.at.toISOString()}`}
                    x={Math.min(
                      Math.max((left + right) / 2, pad),
                      width - pad,
                    )}
                    y={20}
                    className="axis-major"
                  >
                    {tick.label}
                  </text>
                );
              })}
              {tiers.minor.map((tick, index) => {
                const next = tiers.minor[index + 1];
                const right = next ? next.x : width;
                return (
                  <text
                    key={`mL${tick.at.toISOString()}`}
                    x={(tick.x + right) / 2}
                    y={38}
                    className="axis-minor"
                  >
                    {tick.label}
                  </text>
                );
              })}
              <line
                x1={0}
                x2={width}
                y1={AXIS}
                y2={AXIS}
                className="axis-rule"
              />
            </g>

            <g transform={`translate(0, ${AXIS})`}>
              {/* Skipped when the buffer sits entirely beyond the frame, which
                  it does whenever the case is running well ahead of plan. */}
              {detail.buffer_seconds > 0 &&
                timeToX(viewport, planDeadline) < width && (
                  <BufferBlock
                    viewport={viewport}
                    from={planDeadline}
                    to={new Date(detail.target_date)}
                    height={height}
                    consumed={detail.buffer_consumed_ratio ?? 0}
                  />
                )}

              {/* A rule under each group, as in the reference: it separates the
                  phases without needing a box around each one. */}
              {rows.map((row, index) =>
                row.kind === "summary" && index > 0 ? (
                  <line
                    key={`sep-${row.key}`}
                    x1={0}
                    x2={width}
                    y1={index * ROW_HEIGHT}
                    y2={index * ROW_HEIGHT}
                    className="group-rule"
                  />
                ) : null,
              )}

              <g className="gantt-deps">
                {detail.dependencies.map((edge) => {
                  const from = detail.tasks.find(
                    (task) => task.name === edge.predecessor,
                  );
                  const to = detail.tasks.find(
                    (task) => task.name === edge.successor,
                  );
                  const fromRow = rowOf.get(edge.predecessor);
                  const toRow = rowOf.get(edge.successor);
                  if (
                    !from?.forecast_end ||
                    !to?.forecast_start ||
                    fromRow === undefined ||
                    toRow === undefined
                  ) {
                    return null;
                  }
                  const start = {
                    x: timeToX(viewport, from.forecast_end),
                    y: rowCentre(fromRow),
                  };
                  const finish = {
                    x: timeToX(viewport, to.forecast_start),
                    y: rowCentre(toRow),
                  };
                  const dimmed =
                    hovered !== null &&
                    !connected.has(edge.predecessor) &&
                    !connected.has(edge.successor);
                  return (
                    <g
                      key={`${edge.predecessor}->${edge.successor}`}
                      className={[
                        "dep",
                        `hue-${colourOf.get(from.phase || "") ?? 0}`,
                        dimmed ? "dimmed" : "",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                    >
                      {/* Waiting is not work, so a lagged edge is drawn as a
                          dashed run rather than getting its own line beneath the
                          connector -- the two ran along the same row and the
                          dashes simply disappeared under the solid stroke. */}
                      <path
                        d={elbowPath(start, finish)}
                        className={
                          edge.lag_seconds > 0
                            ? "dep-line is-lagged"
                            : "dep-line"
                        }
                      />
                      {edge.lag_seconds > 0 && (
                        <text
                          x={(start.x + finish.x) / 2}
                          y={start.y - 5}
                          className="dep-lag-label"
                        >
                          {formatSpan(edge.lag_seconds)}
                        </text>
                      )}
                      <circle cx={finish.x} cy={finish.y} r={2.5} />
                    </g>
                  );
                })}
              </g>

              {/* No bar for a phase row. It spanned its tasks and said nothing
                  they did not already say -- the rail names the phase and the
                  rule separates it, which is the whole job. */}
              {rows.map((row, index) =>
                row.kind === "summary" ? null : (
                  <TaskBars
                    key={row.task.id}
                    task={row.task}
                    colour={row.colour}
                    row={index}
                    viewport={viewport}
                    dimmed={
                      hovered !== null && !connected.has(row.task.name)
                    }
                    showCriticalPath={showCriticalPath}
                    onSelect={onSelect}
                    onHover={setHovered}
                  />
                ),
              )}

              <Marker
                viewport={viewport}
                at={new Date()}
                height={height}
                className="marker-now"
                label="now"
              />
              <Marker
                viewport={viewport}
                at={new Date(detail.target_date)}
                height={height}
                className="marker-target"
                label="target"
              />
              {detail.target_date_history[0] && (
                <Marker
                  viewport={viewport}
                  at={new Date(detail.target_date_history[0].from)}
                  height={height}
                  className="marker-original"
                  label="original"
                />
              )}
            </g>
          </svg>
        </div>
      </div>
    </div>
  );
}

function TaskBars({
  task,
  colour,
  row,
  viewport,
  dimmed,
  showCriticalPath,
  onSelect,
  onHover,
}: {
  task: Task;
  colour: number;
  row: number;
  viewport: Viewport;
  dimmed: boolean;
  showCriticalPath: boolean;
  onSelect: (task: Task) => void;
  onHover: (name: string | null) => void;
}) {
  const delta = variance(task);
  const late = delta !== null && delta > 0;

  const baselineVisible =
    task.baseline_start !== null &&
    task.baseline_end !== null &&
    timeToX(viewport, task.baseline_end) > 0 &&
    timeToX(viewport, task.baseline_start) < viewport.width;

  return (
    <g
      className={[
        "bars",
        `hue-${colour}`,
        `is-${task.status}`,
        dimmed ? "dimmed" : "",
        showCriticalPath && task.is_on_critical_path ? "critical" : "",
        task.is_optional ? "optional" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      onMouseEnter={() => onHover(task.name)}
      onMouseLeave={() => onHover(null)}
      onClick={() => onSelect(task)}
    >
      {/* A task ticked off without ever being started occupies no time we know
          of, so it is drawn as a milestone. A bar would have to claim a
          duration, and one of no width is simply invisible. */}
      {isInstant(task) ? (
        <Milestone
          x={timeToX(viewport, task.forecast_end!)}
          y={rowCentre(row)}
        />
      ) : (
        task.forecast_start &&
        task.forecast_end && (
          <rect
            x={timeToX(viewport, task.forecast_start)}
            y={barY(row)}
            width={spanWidth(
              viewport,
              task.forecast_start,
              task.forecast_end,
            )}
            height={BAR_HEIGHT}
            rx={BAR_RADIUS}
            className="bar bar-task"
          />
        )
      )}

      {/* No baseline means the task was inserted after the case began, so
          there is nothing to compare it against (§5.10). One that falls wholly
          outside the frame is dropped rather than clipped to the edge: it is a
          reference for the bar it sits under, and a stub with no bar near it
          reads as debris. */}
      {task.baseline_start && task.baseline_end && baselineVisible && (
        <rect
          x={timeToX(viewport, task.baseline_start)}
          y={baselineY(row)}
          width={spanWidth(viewport, task.baseline_start, task.baseline_end)}
          height={BASELINE_HEIGHT}
          rx={BASELINE_HEIGHT / 2}
          className="bar bar-baseline"
        />
      )}

      {/* Only lateness is labelled. Running early against an as-late-as-
          possible plan just means the task has slack, and printing "−19d 13h"
          beside four of six bars buried the one number worth reading. The
          full variance stays in the list view, where it has a column. */}
      {late && delta >= 3600 && task.forecast_end && (
        <text
          x={timeToX(viewport, task.forecast_end) + 7}
          y={rowCentre(row) + 3}
          className="delta delta-late"
        >
          +{formatSpan(delta)}
        </text>
      )}
    </g>
  );
}

/** A point in time rather than a span: the conventional Gantt diamond. */
function Milestone({ x, y }: { x: number; y: number }) {
  const r = BAR_HEIGHT / 2 + 1;
  return (
    <path
      d={`M ${x} ${y - r} L ${x + r} ${y} L ${x} ${y + r} L ${x - r} ${y} Z`}
      className="bar bar-milestone"
    />
  );
}

function BufferBlock({
  viewport,
  from,
  to,
  height,
  consumed,
}: {
  viewport: Viewport;
  from: Date;
  to: Date;
  height: number;
  consumed: number;
}) {
  const x = timeToX(viewport, from);
  const full = spanWidth(viewport, from, to);
  const eaten = Math.min(Math.max(consumed, 0), 1) * full;
  return (
    <g className="buffer">
      <rect x={x} y={0} width={full} height={height} className="buffer-block" />
      {/* The consumed portion is filled, so "how much is left to burn" is
          readable without doing arithmetic. */}
      {eaten > 0 && (
        <rect
          x={x}
          y={0}
          width={eaten}
          height={height}
          className="buffer-consumed"
        />
      )}
    </g>
  );
}

function Marker({
  viewport,
  at,
  height,
  className,
  label,
}: {
  viewport: Viewport;
  at: Date;
  height: number;
  className: string;
  label: string;
}) {
  const x = timeToX(viewport, at);

  // Pinned to the edge it fell off rather than dropped. On a case whose work
  // starts months out, "now" is off the left of the frame, and simply not
  // drawing it left no way to tell whether today was before or after the plan.
  if (x < 0 || x > viewport.width) {
    const before = x < 0;
    const edge = before ? 0 : viewport.width;
    // How far off, not just which side. "target ▶" alone hides the three
    // weeks of slack that put it off the frame in the first place.
    const away = before
      ? viewport.from.getTime() - at.getTime()
      : at.getTime() - viewport.to.getTime();
    const gap = formatSpan(away / 1000);
    return (
      <g className={`${className} off-frame`}>
        <line x1={edge} x2={edge} y1={0} y2={height} />
        <text
          x={before ? edge + 6 : edge - 6}
          y={12}
          textAnchor={before ? "start" : "end"}
        >
          {before ? `◀ ${gap} ${label}` : `${label} ${gap} ▶`}
        </text>
      </g>
    );
  }

  return (
    <g className={className}>
      <line x1={x} x2={x} y1={0} y2={height} />
      <text x={x + 5} y={12}>
        {label}
      </text>
    </g>
  );
}

/**
 * Who a phase belongs to, shown above its summary bar.
 *
 * The most common owner rather than a list: the label answers "who do I go to
 * about this phase", and three names would answer nothing.
 */
function ownerLabel(detail: CaseDetail, phase: string): string | null {
  const counts = new Map<number, number>();
  for (const task of detail.tasks) {
    if ((task.phase || "") !== phase || task.owner_id === null) continue;
    counts.set(task.owner_id, (counts.get(task.owner_id) ?? 0) + 1);
  }
  if (counts.size === 0) return null;
  const [top] = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  return detail.people[String(top![0])] ?? null;
}

/** Everything upstream and downstream, for the hover focus effect. */
function relatedTasks(detail: CaseDetail, name: string | null): Set<string> {
  if (!name) return new Set();
  const found = new Set<string>([name]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const edge of detail.dependencies) {
      if (found.has(edge.predecessor) && !found.has(edge.successor)) {
        found.add(edge.successor);
        grew = true;
      }
      if (found.has(edge.successor) && !found.has(edge.predecessor)) {
        found.add(edge.predecessor);
        grew = true;
      }
    }
  }
  return found;
}

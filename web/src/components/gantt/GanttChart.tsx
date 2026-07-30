/**
 * SVG Gantt renderer (implement.md §9.2, design.md §4).
 *
 * Built rather than borrowed: the chart needs a phase summary bar over its own
 * tasks, a baseline ghost under each forecast, orthogonal connectors that stay
 * legible when several converge, and a buffer block at the tail. Libraries
 * assume one bar per row and stop there.
 *
 * Colour encodes **phase**, not status. Phase is what makes a chart readable
 * from across the room, and status is still carried by the glyph in the rail,
 * the bar's fill treatment, and its label -- never by colour alone (§4.5).
 *
 * The canvas keeps its dark palette in both app themes. It is a distinct
 * surface, like a map or an editor, and the pale-on-white version of the same
 * design loses the separation between the plot and the page.
 */

import { useMemo, useState } from "react";

import type { CaseDetail, Task } from "../../api/types";
import { STATUS_META, formatDuration, variance } from "../../lib/format";
import {
  BAR_HEIGHT,
  BASELINE_HEIGHT,
  ROW_HEIGHT,
  SUMMARY_HEIGHT,
  type Viewport,
  axisTiers,
  baselineY,
  barY,
  elbowPath,
  groupSpans,
  paddedRange,
  rowCentre,
  spanWidth,
  summaryY,
  timeToX,
} from "../../lib/ganttLayout";

const RAIL = 210;
const AXIS = 46;
/** Phase palette; index cycles, so a sixth phase reuses the first colour. */
const PALETTE = 6;

interface Props {
  detail: CaseDetail;
  onSelect: (task: Task) => void;
  selectedId?: number | null;
  showCriticalPath?: boolean;
}

type Row =
  | { kind: "summary"; key: string; label: string; colour: number }
  | { kind: "task"; task: Task; colour: number };

export function GanttChart({
  detail,
  onSelect,
  selectedId,
  showCriticalPath = true,
}: Props) {
  const [width, setWidth] = useState(880);
  const [hovered, setHovered] = useState<string | null>(null);

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
          });
        }
        current = key;
      }
      out.push({ kind: "task", task, colour });
    }
    return out;
  }, [detail.tasks, groups, colourOf]);

  const rowOf = useMemo(() => {
    const map = new Map<string, number>();
    rows.forEach((row, index) => {
      if (row.kind === "task") map.set(row.task.name, index);
    });
    return map;
  }, [rows]);

  const viewport: Viewport = useMemo(() => {
    const { from, to } = paddedRange([
      ...detail.tasks.flatMap((task) => [
        task.baseline_start,
        task.baseline_end,
        task.forecast_start,
        task.forecast_end,
      ]),
      detail.target_date,
      new Date().toISOString(),
    ]);
    return { from, to, width };
  }, [detail.tasks, detail.target_date, width]);

  const tiers = useMemo(() => axisTiers(viewport), [viewport]);
  const height = rows.length * ROW_HEIGHT;
  const connected = useMemo(
    () => relatedTasks(detail, hovered),
    [detail, hovered],
  );

  const planDeadline = new Date(
    new Date(detail.target_date).getTime() - detail.buffer_seconds * 1000,
  );

  return (
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
              {row.label}
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

      <div
        className="gantt-canvas"
        ref={(node) => {
          if (node && node.clientWidth && node.clientWidth !== width) {
            setWidth(node.clientWidth);
          }
        }}
      >
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
              const right = next ? next.x : width;
              return (
                <text
                  key={`ML${tick.at.toISOString()}`}
                  x={(Math.max(tick.x, 0) + right) / 2}
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
            {detail.buffer_seconds > 0 && (
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
                    {edge.lag_seconds > 0 && (
                      <>
                        {/* Waiting is not work: a solid run across a four-hour
                            handover would read as somebody idling. */}
                        <line
                          x1={start.x}
                          x2={finish.x}
                          y1={start.y}
                          y2={start.y}
                          className="dep-lag"
                        />
                        <text
                          x={(start.x + finish.x) / 2}
                          y={start.y - 5}
                          className="dep-lag-label"
                        >
                          {formatDuration(edge.lag_seconds)}
                        </text>
                      </>
                    )}
                    <path d={elbowPath(start, finish)} className="dep-line" />
                    <circle cx={finish.x} cy={finish.y} r={2.5} />
                  </g>
                );
              })}
            </g>

            {rows.map((row, index) =>
              row.kind === "summary" ? (
                <SummaryBar
                  key={`sb-${row.key}`}
                  span={groups.find((group) => group.key === row.key)!}
                  row={index}
                  viewport={viewport}
                  owner={ownerLabel(detail, row.key)}
                />
              ) : (
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
  );
}

function SummaryBar({
  span,
  row,
  viewport,
  owner,
}: {
  span: { from: Date; to: Date; colour: number };
  row: number;
  viewport: Viewport;
  owner: string | null;
}) {
  const x = timeToX(viewport, span.from);
  return (
    <g className={`summary hue-${span.colour}`}>
      {owner && (
        <text x={x} y={summaryY(row) - 5} className="summary-owner">
          {owner}
        </text>
      )}
      <rect
        x={x}
        y={summaryY(row)}
        width={spanWidth(viewport, span.from, span.to)}
        height={SUMMARY_HEIGHT}
        rx={SUMMARY_HEIGHT / 2}
        className="bar bar-summary"
      />
    </g>
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
      {task.forecast_start && task.forecast_end && (
        <rect
          x={timeToX(viewport, task.forecast_start)}
          y={barY(row)}
          width={spanWidth(
            viewport,
            task.forecast_start,
            task.forecast_end,
          )}
          height={BAR_HEIGHT}
          rx={BAR_HEIGHT / 2}
          className="bar bar-task"
        />
      )}

      {/* No baseline means the task was inserted after the case began, so
          there is nothing to compare it against (§5.10). */}
      {task.baseline_start && task.baseline_end && (
        <rect
          x={timeToX(viewport, task.baseline_start)}
          y={baselineY(row)}
          width={spanWidth(viewport, task.baseline_start, task.baseline_end)}
          height={BASELINE_HEIGHT}
          rx={BASELINE_HEIGHT / 2}
          className="bar bar-baseline"
        />
      )}

      {delta !== null && Math.abs(delta) >= 3600 && task.forecast_end && (
        <text
          x={timeToX(viewport, task.forecast_end) + 7}
          y={rowCentre(row) + 3}
          className={late ? "delta delta-late" : "delta"}
        >
          {late ? "+" : "−"}
          {formatDuration(Math.abs(delta))}
        </text>
      )}
    </g>
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
  if (x < 0 || x > viewport.width) return null;
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

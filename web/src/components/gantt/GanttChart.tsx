/**
 * SVG Gantt renderer (implement.md §9.2, design.md §4).
 *
 * Built rather than borrowed because of the dual track: every task needs a
 * baseline bar and a forecast bar with independent styling, and the common
 * libraries assume one bar per row. Multi-predecessor connectors, the buffer
 * block and the insert affordance on a dependency line all need the same level
 * of control.
 */

import { useMemo, useState } from "react";

import type { CaseDetail, Task } from "../../api/types";
import { STATUS_META, formatDuration, variance } from "../../lib/format";
import {
  BAR_HEIGHT,
  ROW_HEIGHT,
  type Scale,
  type Viewport,
  baselineY,
  dependencyPath,
  forecastY,
  paddedRange,
  pickScale,
  rowCentre,
  spanWidth,
  ticks,
  timeToX,
} from "../../lib/ganttLayout";

const TASK_COLUMN = 260;
const AXIS_HEIGHT = 34;

interface Props {
  detail: CaseDetail;
  onSelect: (task: Task) => void;
  selectedId?: number | null;
  showCriticalPath?: boolean;
  groupByPhase?: boolean;
}

export function GanttChart({
  detail,
  onSelect,
  selectedId,
  showCriticalPath = true,
  groupByPhase = true,
}: Props) {
  const [width, setWidth] = useState(900);
  const [hovered, setHovered] = useState<string | null>(null);

  const rows = useMemo(
    () => orderRows(detail.tasks, groupByPhase),
    [detail.tasks, groupByPhase],
  );

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

  const scale: Scale = useMemo(
    () => pickScale(viewport.from, viewport.to),
    [viewport.from, viewport.to],
  );

  const rowIndex = new Map(rows.map((row, index) => [row.task?.name, index]));
  const height = rows.length * ROW_HEIGHT;

  /** Upstream and downstream of the hovered task, for the focus effect. */
  const connected = useMemo(
    () => relatedTasks(detail, hovered),
    [detail, hovered],
  );

  const planDeadline = new Date(
    new Date(detail.target_date).getTime() - detail.buffer_seconds * 1000,
  );

  return (
    <div className="gantt">
      <div className="gantt-rail" style={{ width: TASK_COLUMN }}>
        <div className="gantt-axis-spacer" style={{ height: AXIS_HEIGHT }} />
        {rows.map((row, index) =>
          row.kind === "phase" ? (
            <div key={`phase-${row.phase}`} className="gantt-phase">
              {row.phase}
            </div>
          ) : (
            <button
              key={row.task.id}
              type="button"
              className={rowClass(row.task, selectedId, connected, hovered)}
              style={{ height: ROW_HEIGHT }}
              onClick={() => onSelect(row.task)}
              onMouseEnter={() => setHovered(row.task.name)}
              onMouseLeave={() => setHovered(null)}
            >
              <span className={`status status-${row.task.status}`}>
                {STATUS_META[row.task.status].icon}
              </span>
              <span className="gantt-row-name">
                {row.task.display_name || row.task.name}
              </span>
              {rowBadges(row.task, showCriticalPath)}
              <span className="gantt-row-index">{index}</span>
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
          height={height + AXIS_HEIGHT}
          role="img"
          aria-label="Case schedule"
        >
          <g className="gantt-axis">
            {ticks(viewport, scale).map((tick) => (
              <g key={tick.at.toISOString()}>
                <line
                  x1={tick.x}
                  x2={tick.x}
                  y1={AXIS_HEIGHT - 8}
                  y2={height + AXIS_HEIGHT}
                  className={tick.major ? "tick-major" : "tick-minor"}
                />
                <text x={tick.x + 4} y={16} className="tick-label">
                  {tick.label}
                </text>
              </g>
            ))}
          </g>

          <g transform={`translate(0, ${AXIS_HEIGHT})`}>
            {detail.buffer_seconds > 0 && (
              <BufferBlock
                viewport={viewport}
                from={planDeadline}
                to={new Date(detail.target_date)}
                height={height}
                consumed={detail.buffer_consumed_ratio ?? 0}
              />
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
            {detail.target_date_history.length > 0 && (
              <Marker
                viewport={viewport}
                at={new Date(detail.target_date_history[0]!.from)}
                height={height}
                className="marker-original"
                label="original target"
              />
            )}

            <g className="gantt-deps">
              {detail.dependencies.map((edge) => {
                const from = detail.tasks.find(
                  (task) => task.name === edge.predecessor,
                );
                const to = detail.tasks.find(
                  (task) => task.name === edge.successor,
                );
                const fromRow = rowIndex.get(edge.predecessor);
                const toRow = rowIndex.get(edge.successor);
                if (
                  !from?.forecast_end ||
                  !to?.forecast_start ||
                  fromRow === undefined ||
                  toRow === undefined
                ) {
                  return null;
                }
                const path = dependencyPath(
                  viewport,
                  from.forecast_end,
                  fromRow,
                  to.forecast_start,
                  toRow,
                  edge.lag_seconds,
                  formatDuration(edge.lag_seconds),
                );
                const dimmed =
                  hovered !== null &&
                  !connected.has(edge.predecessor) &&
                  !connected.has(edge.successor);
                return (
                  <g
                    key={`${edge.predecessor}->${edge.successor}`}
                    className={dimmed ? "dep dimmed" : "dep"}
                  >
                    {path.lag && (
                      <>
                        <line
                          x1={path.lag.x1}
                          x2={path.lag.x2}
                          y1={path.lag.y}
                          y2={path.lag.y}
                          className="dep-lag"
                        />
                        <text
                          x={(path.lag.x1 + path.lag.x2) / 2}
                          y={path.lag.y - 4}
                          className="dep-lag-label"
                        >
                          {path.lag.label}
                        </text>
                      </>
                    )}
                    <path d={path.d} className="dep-line" />
                    <circle cx={path.to.x} cy={path.to.y} r={2.5} />
                  </g>
                );
              })}
            </g>

            {rows.map((row, index) =>
              row.kind === "task" ? (
                <TaskBars
                  key={row.task.id}
                  task={row.task}
                  row={index}
                  viewport={viewport}
                  dimmed={
                    hovered !== null && !connected.has(row.task.name)
                  }
                  showCriticalPath={showCriticalPath}
                  onSelect={onSelect}
                  onHover={setHovered}
                />
              ) : null,
            )}
          </g>
        </svg>
      </div>
    </div>
  );
}

function TaskBars({
  task,
  row,
  viewport,
  dimmed,
  showCriticalPath,
  onSelect,
  onHover,
}: {
  task: Task;
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
        dimmed ? "dimmed" : "",
        showCriticalPath && task.is_on_critical_path ? "critical" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      onMouseEnter={() => onHover(task.name)}
      onMouseLeave={() => onHover(null)}
      onClick={() => onSelect(task)}
    >
      {/* Baseline: the original plan, which never moves. Absent for a task
          inserted after creation, which is drawn single-track instead. */}
      {task.baseline_start && task.baseline_end && (
        <rect
          x={timeToX(viewport, task.baseline_start)}
          y={baselineY(row)}
          width={spanWidth(viewport, task.baseline_start, task.baseline_end)}
          height={BAR_HEIGHT}
          rx={2}
          className="bar bar-baseline"
        />
      )}

      {task.forecast_start && task.forecast_end && (
        <rect
          x={timeToX(viewport, task.forecast_start)}
          y={task.is_unplanned ? rowCentre(row) - BAR_HEIGHT / 2 : forecastY(row)}
          width={spanWidth(viewport, task.forecast_start, task.forecast_end)}
          height={BAR_HEIGHT}
          rx={2}
          className={[
            "bar",
            `bar-${task.status}`,
            task.is_optional ? "bar-optional" : "",
            late ? "bar-late" : "",
          ]
            .filter(Boolean)
            .join(" ")}
        />
      )}

      {delta !== null && Math.abs(delta) >= 3600 && (
        <text
          x={
            timeToX(viewport, task.forecast_end!) +
            6
          }
          y={rowCentre(row) + 4}
          className={late ? "delta delta-late" : "delta"}
        >
          {late ? "+" : "-"}
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
  // The consumed portion is filled in, so "how much is left to burn" is
  // readable at a glance (design.md §4.1).
  const eaten = Math.min(Math.max(consumed, 0), 1) * full;
  return (
    <g className="buffer">
      <rect x={x} y={0} width={full} height={height} className="buffer-block" />
      {eaten > 0 && (
        <rect
          x={x}
          y={0}
          width={eaten}
          height={height}
          className="buffer-consumed"
        />
      )}
      <text x={x + 4} y={12} className="buffer-label">
        buffer
      </text>
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
      <text x={x + 4} y={height - 4}>
        {label}
      </text>
    </g>
  );
}

type Row =
  | { kind: "phase"; phase: string; task?: undefined }
  | { kind: "task"; task: Task; phase: string };

/** Order rows, inserting phase headings when grouping is on. */
function orderRows(tasks: Task[], groupByPhase: boolean): Row[] {
  if (!groupByPhase) {
    return tasks.map((task) => ({
      kind: "task" as const,
      task,
      phase: task.phase,
    }));
  }
  const rows: Row[] = [];
  let current: string | null = null;
  for (const task of tasks) {
    if (task.phase && task.phase !== current) {
      rows.push({ kind: "phase", phase: task.phase });
      current = task.phase;
    }
    rows.push({ kind: "task", task, phase: task.phase });
  }
  return rows;
}

/**
 * Everything upstream and downstream of a task.
 *
 * Hovering highlights the whole chain rather than one row, because "what does
 * this block and what blocks it" is the question being asked.
 */
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

function rowClass(
  task: Task,
  selectedId: number | null | undefined,
  connected: Set<string>,
  hovered: string | null,
): string {
  return [
    "gantt-row",
    task.id === selectedId ? "selected" : "",
    hovered !== null && !connected.has(task.name) ? "dimmed" : "",
  ]
    .filter(Boolean)
    .join(" ");
}

function rowBadges(task: Task, showCriticalPath: boolean) {
  return (
    <span className="gantt-row-badges">
      {showCriticalPath && task.is_on_critical_path && (
        <span className="badge badge-critical" title="On the critical path">
          !
        </span>
      )}
      {task.is_optional && (
        <span className="badge" title="Optional: the case can close without it">
          ◇
        </span>
      )}
      {task.is_unplanned && (
        <span className="badge" title="Added after the case was created">
          ＋
        </span>
      )}
      {task.task_api && (
        <span className="badge" title={`Automated via ${task.task_api}`}>
          🔌
        </span>
      )}
    </span>
  );
}

/** Case detail: the Gantt plus the task drawer (design.md §4, §5, §7). */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError, api } from "../../api/client";
import type { CaseDetail as Detail, Task } from "../../api/types";
import { GanttChart } from "../../components/gantt/GanttChart";
import {
  HEALTH_META,
  STATUS_META,
  formatDelta,
  formatDuration,
  formatMoment,
  formatPercent,
  isLateStart,
  variance,
} from "../../lib/format";

export function CaseDetailPage() {
  const { id } = useParams();
  const caseId = Number(id);
  const client = useQueryClient();

  const [selected, setSelected] = useState<Task | null>(null);
  const [view, setView] = useState<"gantt" | "list">("gantt");
  const [showCritical, setShowCritical] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const detail = useQuery({
    queryKey: ["case", caseId],
    queryFn: () => api.caseDetail(caseId),
    enabled: Number.isFinite(caseId),
  });

  const refresh = (next: Detail) => {
    client.setQueryData(["case", caseId], next);
    client.invalidateQueries({ queryKey: ["cases"] });
    client.invalidateQueries({ queryKey: ["case-summary"] });
    const updated = next.tasks.find((task) => task.id === selected?.id);
    setSelected(updated ?? null);
  };

  const onError = (err: unknown) =>
    setError(
      err instanceof ApiError ? `${err.code}: ${err.message}` : String(err),
    );

  const complete = useMutation({
    mutationFn: (payload: { taskId: number; note: string }) =>
      api.completeTask(caseId, payload.taskId, { note: payload.note }),
    onSuccess: refresh,
    onError,
  });

  const update = useMutation({
    mutationFn: (payload: { taskId: number; body: unknown }) =>
      api.updateTask(caseId, payload.taskId, payload.body),
    onSuccess: refresh,
    onError,
  });

  if (detail.isLoading) return <p className="muted page">Loading…</p>;
  if (detail.error) {
    return (
      <p className="page tone-danger">
        {detail.error instanceof ApiError
          ? detail.error.message
          : "Could not load this case"}
      </p>
    );
  }
  const data = detail.data!;
  const late = data.exceeds_target_by_seconds > 0;

  return (
    <section className="page">
      <Link to="/cases" className="back">
        ← Cases
      </Link>

      <header className="case-head">
        <div>
          <h1>{data.name}</h1>
          <p className="muted">
            {data.template_name} v{data.template_version} · target{" "}
            {formatMoment(data.target_date)}
            {data.target_date_history.length > 0 && (
              <span className="pill" title="Target date has been changed">
                target moved ×{data.target_date_history.length}
              </span>
            )}
          </p>
        </div>
        {data.health && (
          <span className={`chip tone-${HEALTH_META[data.health].tone}`}>
            {HEALTH_META[data.health].icon} {HEALTH_META[data.health].label}
          </span>
        )}
      </header>

      <div className="case-metrics">
        <div>
          <span className="muted small">Progress</span>
          <div className="bar-track">
            <div
              className="bar-fill"
              style={{ width: `${(data.progress_ratio ?? 0) * 100}%` }}
            />
          </div>
          {/* Weighted percentage plus the raw count: the first reflects real
              effort, the second how many things are still open. */}
          <strong>
            {formatPercent(data.progress_ratio)} ·{" "}
            {data.tasks.filter((task) => task.status === "done").length}/
            {data.tasks.length}
          </strong>
        </div>
        <div>
          <span className="muted small">Forecast</span>
          <strong className={late ? "tone-danger" : ""}>
            {formatMoment(data.forecast_end)}{" "}
            {late && `(${formatDelta(data.exceeds_target_by_seconds)})`}
          </strong>
        </div>
        {data.buffer_seconds > 0 && (
          <div>
            <span className="muted small">Buffer used</span>
            <strong>
              {formatPercent(data.buffer_consumed_ratio)} of{" "}
              {formatDuration(data.buffer_seconds)}
            </strong>
          </div>
        )}
      </div>

      <div className="toolbar">
        <div className="segmented">
          <button
            type="button"
            className={view === "gantt" ? "active" : ""}
            onClick={() => setView("gantt")}
          >
            Gantt
          </button>
          <button
            type="button"
            className={view === "list" ? "active" : ""}
            onClick={() => setView("list")}
          >
            List
          </button>
        </div>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={showCritical}
            onChange={(event) => setShowCritical(event.target.checked)}
          />
          Highlight critical path
        </label>
      </div>

      {error && (
        <div className="banner tone-danger" role="alert">
          {error}
          <button type="button" onClick={() => setError(null)}>
            ✕
          </button>
        </div>
      )}

      {view === "gantt" ? (
        <GanttChart
          detail={data}
          onSelect={setSelected}
          selectedId={selected?.id ?? null}
          showCriticalPath={showCritical}
        />
      ) : (
        <TaskTable tasks={data.tasks} onSelect={setSelected} />
      )}

      {data.skipped_tasks.length > 0 && (
        <div className="banner">
          {/* Deliberately visible: someone comparing against the template needs
              to know a step was filtered out, not deleted (design.md §4.6). */}
          ⓘ {data.skipped_tasks.length} step
          {data.skipped_tasks.length > 1 ? "s" : ""} skipped by this case's
          parameters:{" "}
          {data.skipped_tasks
            .map((entry) => entry.label || entry.id)
            .join(", ")}
        </div>
      )}

      {selected && (
        <TaskDrawer
          task={selected}
          detail={data}
          busy={complete.isPending || update.isPending}
          onClose={() => setSelected(null)}
          onComplete={(note) =>
            complete.mutate({ taskId: selected.id, note })
          }
          onSaveDuration={(seconds) =>
            update.mutate({
              taskId: selected.id,
              body: {
                duration_seconds: seconds,
                expected_version: selected.version,
              },
            })
          }
        />
      )}
    </section>
  );
}

function TaskTable({
  tasks,
  onSelect,
}: {
  tasks: Task[];
  onSelect: (task: Task) => void;
}) {
  return (
    <table className="table">
      <thead>
        <tr>
          <th>Task</th>
          <th>Status</th>
          <th>Phase</th>
          <th>Planned</th>
          <th>Forecast</th>
          <th>Variance</th>
        </tr>
      </thead>
      <tbody>
        {tasks.map((task) => {
          const delta = variance(task);
          return (
            <tr key={task.id} onClick={() => onSelect(task)}>
              <td className="strong">{task.display_name || task.name}</td>
              <td>
                <span className={`status status-${task.status}`}>
                  {STATUS_META[task.status].icon}{" "}
                  {STATUS_META[task.status].label}
                </span>
                {isLateStart(task.status, task.baseline_start) && (
                  <span className="pill tone-warning">not started</span>
                )}
              </td>
              <td className="muted">{task.phase || "—"}</td>
              <td className="muted">
                {formatMoment(task.baseline_start)} →{" "}
                {formatMoment(task.baseline_end)}
              </td>
              <td>
                {formatMoment(task.forecast_start)} →{" "}
                {formatMoment(task.forecast_end)}
              </td>
              <td className={delta && delta > 0 ? "tone-danger" : ""}>
                {/* An unplanned task has no baseline, so there is no variance
                    to report rather than a misleading zero. */}
                {delta === null ? "—" : formatDelta(delta)}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function TaskDrawer({
  task,
  detail,
  busy,
  onClose,
  onComplete,
  onSaveDuration,
}: {
  task: Task;
  detail: Detail;
  busy: boolean;
  onClose: () => void;
  onComplete: (note: string) => void;
  onSaveDuration: (seconds: number) => void;
}) {
  const [hours, setHours] = useState(task.duration_seconds / 3600);
  const [note, setNote] = useState("");
  const changed = hours * 3600 !== task.duration_seconds;
  const predecessors = detail.dependencies.filter(
    (edge) => edge.successor === task.name,
  );
  const successors = detail.dependencies.filter(
    (edge) => edge.predecessor === task.name,
  );

  return (
    <aside className="drawer" aria-label={`Task ${task.name}`}>
      <header>
        <div>
          <h2>{task.display_name || task.name}</h2>
          <p className="muted small">
            <span className={`status status-${task.status}`}>
              {STATUS_META[task.status].icon}{" "}
              {STATUS_META[task.status].label}
            </span>
            {task.task_api && ` · 🔌 ${task.task_api}`}
          </p>
        </div>
        <button type="button" onClick={onClose} aria-label="Close">
          ✕
        </button>
      </header>

      <dl className="drawer-fields">
        <dt>Duration</dt>
        <dd>
          <input
            type="number"
            min={0}
            step={0.5}
            value={hours}
            disabled={!task.permissions.can_edit}
            onChange={(event) => setHours(Number(event.target.value))}
          />{" "}
          hours
          {changed && (
            <button
              type="button"
              className="button primary small"
              disabled={busy}
              onClick={() => onSaveDuration(Math.round(hours * 3600))}
            >
              Save
            </button>
          )}
        </dd>

        <dt>Owner</dt>
        <dd>
          {task.owner_id ?? "unassigned"}
          {/* Where the owner came from matters: changing it here affects only
              this task, not everyone holding the same role (design.md §5). */}
          {task.owner_source.startsWith("role:") && (
            <span className="muted small">
              {" "}
              from role {task.owner_source.slice(5)}
            </span>
          )}
          {task.owner_source === "manual" && (
            <span className="muted small"> set manually</span>
          )}
        </dd>

        <dt>Planned</dt>
        <dd>
          {task.is_unplanned ? (
            <span className="muted">added after creation — no baseline</span>
          ) : (
            `${formatMoment(task.baseline_start)} → ${formatMoment(
              task.baseline_end,
            )}`
          )}
        </dd>

        <dt>Forecast</dt>
        <dd>
          {formatMoment(task.forecast_start)} →{" "}
          {formatMoment(task.forecast_end)}
        </dd>

        {task.actual_start && (
          <>
            <dt>Actual</dt>
            <dd>
              {formatMoment(task.actual_start)} →{" "}
              {task.actual_end ? formatMoment(task.actual_end) : "in progress"}
            </dd>
          </>
        )}

        <dt>Depends on</dt>
        <dd>
          {predecessors.length === 0
            ? "nothing"
            : predecessors
                .map(
                  (edge) =>
                    edge.predecessor +
                    (edge.lag_seconds
                      ? ` (+${formatDuration(edge.lag_seconds)} wait)`
                      : ""),
                )
                .join(", ")}
        </dd>

        <dt>Blocks</dt>
        <dd>
          {successors.length === 0
            ? "nothing"
            : successors.map((edge) => edge.successor).join(", ")}
        </dd>
      </dl>

      {task.permissions.can_complete && task.status !== "done" && (
        <div className="drawer-action">
          <label>
            Note
            <textarea
              rows={2}
              value={note}
              onChange={(event) => setNote(event.target.value)}
              placeholder="Optional"
            />
          </label>
          <button
            type="button"
            className="button primary"
            disabled={busy}
            onClick={() => onComplete(note)}
          >
            ✓ Mark complete
          </button>
        </div>
      )}

      {!task.permissions.can_complete && task.status !== "done" && (
        <p className="muted small">
          {/* Fields stay visible when read-only; hiding them would leave the
              viewer unable to see what the task even is (design.md §5). */}
          You cannot complete this task — only its owner or their group can.
        </p>
      )}
    </aside>
  );
}

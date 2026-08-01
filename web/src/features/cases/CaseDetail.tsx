/** Case detail: the Gantt plus the task drawer (design.md §4, §5, §7). */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError, api } from "../../api/client";
import type { CaseDetail as Detail, Task } from "../../api/types";
import { GanttChart } from "../../components/gantt/GanttChart";
import { InsertTaskDialog } from "./InsertTaskDialog";
import {
  HEALTH_META,
  STATUS_META,
  formatDelta,
  formatDuration,
  formatMoment,
  formatPercent,
  formatSpan,
  isInstant,
  isLateStart,
  ownerLabel,
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
  const [inserting, setInserting] = useState(false);
  const [editingTarget, setEditingTarget] = useState(false);

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

  const retry = useMutation({
    mutationFn: (taskId: number) => api.retryTask(caseId, taskId),
    onSuccess: refresh,
    onError,
  });

  const removeTask = useMutation({
    mutationFn: (taskId: number) => api.deleteTask(caseId, taskId),
    onSuccess: (next) => {
      setSelected(null);
      refresh(next);
    },
    onError,
  });

  const moveTarget = useMutation({
    mutationFn: (payload: { target_date: string; note: string }) =>
      api.updateCase(caseId, payload),
    onSuccess: (next) => {
      setEditingTarget(false);
      refresh(next);
    },
    onError,
  });

  const cancelCase = useMutation({
    mutationFn: (note: string) => api.cancelCase(caseId, note),
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
            {data.permissions.can_edit && data.status === "active" && (
              <button
                type="button"
                className="button small"
                onClick={() => setEditingTarget(true)}
              >
                Change
              </button>
            )}
            {data.target_date_history.length > 0 && (
              <span
                className="pill"
                title={data.target_date_history
                  .map((entry) => `${entry.from} → ${entry.to} ${entry.note}`)
                  .join("\n")}
              >
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
        <span className="spacer" />
        {data.status === "active" && data.permissions.can_insert_task && (
          <button
            type="button"
            className="button"
            onClick={() => setInserting(true)}
          >
            + Insert step
          </button>
        )}
        {data.status === "active" && data.permissions.can_cancel && (
          <button
            type="button"
            className="button"
            onClick={() => {
              const note = window.prompt("Why is this case being cancelled?");
              if (note !== null) cancelCase.mutate(note);
            }}
          >
            Cancel case
          </button>
        )}
      </div>

      {editingTarget && (
        <TargetDateForm
          current={data.target_date}
          busy={moveTarget.isPending}
          onCancel={() => setEditingTarget(false)}
          onSave={(target_date, note) =>
            moveTarget.mutate({ target_date, note })
          }
        />
      )}

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

      {inserting && (
        <InsertTaskDialog
          detail={data}
          anchor={selected?.name}
          onClose={() => setInserting(false)}
          onInserted={refresh}
        />
      )}

      {selected && (
        <TaskDrawer
          // Keyed on the task, so selecting a different one remounts the
          // drawer. Without this its `useState` initialisers keep the previous
          // task's values: the duration field showed the task you looked at
          // before, and saving wrote that number onto the one you were
          // looking at.
          key={selected.id}
          caseId={caseId}
          task={selected}
          detail={data}
          busy={
            complete.isPending || update.isPending || retry.isPending
          }
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
          onRetry={() => retry.mutate(selected.id)}
          onDelete={() => {
            if (
              window.confirm(
                `Remove ${selected.name}? Its neighbours will be reconnected.`,
              )
            ) {
              removeTask.mutate(selected.id);
            }
          }}
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
          {/* Without this the windows below look self-contradictory: a
              12-hour task planned across 27 hours is a working calendar
              spanning a night, not an error. */}
          <th>Duration</th>
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
                {formatSpan(task.duration_seconds)}
              </td>
              <td className="muted">
                {formatMoment(task.baseline_start)} →{" "}
                {formatMoment(task.baseline_end)}
              </td>
              <td>
                {/* Completed without a recorded start: one moment, not a
                    window we would be making up. */}
                {isInstant(task) ? (
                  <>
                    {formatMoment(task.forecast_end)}
                    <span className="muted small"> · completed</span>
                  </>
                ) : (
                  <>
                    {formatMoment(task.forecast_start)} →{" "}
                    {formatMoment(task.forecast_end)}
                  </>
                )}
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

function TargetDateForm({
  current,
  busy,
  onCancel,
  onSave,
}: {
  current: string;
  busy: boolean;
  onCancel: () => void;
  onSave: (targetDate: string, note: string) => void;
}) {
  const at = new Date(current);
  const [date, setDate] = useState(
    `${at.getFullYear()}-${String(at.getMonth() + 1).padStart(2, "0")}-${String(
      at.getDate(),
    ).padStart(2, "0")}`,
  );
  const [time, setTime] = useState(
    `${String(at.getHours()).padStart(2, "0")}:${String(
      at.getMinutes(),
    ).padStart(2, "0")}`,
  );
  const [note, setNote] = useState("");

  return (
    <div className="panel">
      <h3>Change the target date</h3>
      <p className="muted small">
        {/* The baseline is the record of what was originally promised, so
            moving the target deliberately leaves it alone (§5.10). */}
        The original plan stays as it is — only the target moves, and the
        change is recorded.
      </p>
      <div className="form">
        <label>
          New target
          <span className="field-row">
            <input
              type="date"
              value={date}
              onChange={(event) => setDate(event.target.value)}
            />
            <input
              type="time"
              value={time}
              step={900}
              onChange={(event) => setTime(event.target.value)}
            />
          </span>
        </label>
        <label>
          Why
          <input
            value={note}
            placeholder="customer moved the date"
            onChange={(event) => setNote(event.target.value)}
          />
        </label>
        <nav className="wizard-nav">
          <button type="button" className="button" onClick={onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className="button primary"
            disabled={busy || !date}
            onClick={() =>
              onSave(new Date(`${date}T${time}`).toISOString(), note)
            }
          >
            Move target
          </button>
        </nav>
      </div>
    </div>
  );
}

function TaskDrawer({
  caseId,
  task,
  detail,
  busy,
  onClose,
  onComplete,
  onSaveDuration,
  onRetry,
  onDelete,
}: {
  caseId: number;
  task: Task;
  detail: Detail;
  busy: boolean;
  onClose: () => void;
  onComplete: (note: string) => void;
  onSaveDuration: (seconds: number) => void;
  onRetry: () => void;
  onDelete: () => void;
}) {
  const [hours, setHours] = useState(task.duration_seconds / 3600);
  const [note, setNote] = useState("");
  const changed = hours * 3600 !== task.duration_seconds;

  // Escape closes it. The button is the obvious way out, but a panel that
  // covers a third of the screen needs one that does not depend on finding
  // anything.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

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
          {ownerLabel(task, detail.people)}
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

      {task.task_api && <RunHistory caseId={caseId} task={task} />}

      <div className="drawer-action">
        {/* Retrying and completing by hand are the two ways out of a failed
            handler; neither should be hidden (design.md §7.2). */}
        {task.status === "failed" && task.permissions.can_retry && (
          <button
            type="button"
            className="button"
            disabled={busy}
            onClick={onRetry}
          >
            ↻ Run again
          </button>
        )}
        {task.permissions.can_edit &&
          task.status !== "done" &&
          task.status !== "running" && (
            <button type="button" className="button" onClick={onDelete}>
              Remove this step
            </button>
          )}
      </div>
    </aside>
  );
}

function RunHistory({ caseId, task }: { caseId: number; task: Task }) {
  const runs = useQuery({
    queryKey: ["runs", caseId, task.id, task.version],
    queryFn: () => api.taskRuns(caseId, task.id),
  });
  if (!runs.data?.length) return null;
  return (
    <div className="panel">
      <h3>Run history</h3>
      {runs.data.map((run) => (
        <p key={run.attempt} className="small">
          <span className={run.status === "failed" ? "tone-danger" : ""}>
            #{run.attempt} {run.status}
          </span>{" "}
          <span className="muted">
            {run.handler_name} · {formatMoment(run.started_at)}
          </span>
          {run.error_message && (
            <>
              <br />
              <span className="tone-danger">{run.error_message}</span>
            </>
          )}
        </p>
      ))}
    </div>
  );
}

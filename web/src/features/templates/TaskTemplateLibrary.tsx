/**
 * Task template library (design.md §9.8, implement.md §3.3, §8.1).
 *
 * Task templates are the reusable building blocks referenced by gantt templates
 * via `uses:`. Listing, creating, editing, and deleting them all live here.
 * Deletion is blocked server-side when any gantt template still references the
 * task template; that error is surfaced as plain text.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, api } from "../../api/client";

interface TaskTemplate {
  name: string;
  display_name: string;
  duration_default: string;
  schedule_mode: string;
  task_api: string | null;
  api_mode: string | null;
  api_config: Record<string, unknown>;
  on_failure: string;
  allow_manual_override: boolean;
}

const FAILURE_LABELS: Record<string, string> = {
  block: "Block (wait for manual fix)",
  continue: "Continue (log and move on)",
  cancel_case: "Cancel the whole case",
};

const API_MODE_LABELS: Record<string, string> = {
  trigger_poll: "Trigger then poll",
  trigger_callback: "Trigger then await callback",
  poll_only: "Poll only (no trigger)",
};

function blank(): TaskTemplate {
  return {
    name: "",
    display_name: "",
    duration_default: "1H",
    schedule_mode: "continuous",
    task_api: null,
    api_mode: "trigger_poll",
    api_config: {},
    on_failure: "block",
    allow_manual_override: true,
  };
}

// ---------------------------------------------------------------------------

interface EditorProps {
  initial: TaskTemplate;
  isNew: boolean;
  handlers: { name: string; builtin: boolean; description: string }[];
  onSave: (t: TaskTemplate) => void;
  onCancel: () => void;
  saving: boolean;
  error: string | null;
}

function Editor({
  initial,
  isNew,
  handlers,
  onSave,
  onCancel,
  saving,
  error,
}: EditorProps) {
  const [form, setForm] = useState<TaskTemplate>(initial);
  const patch = (field: keyof TaskTemplate, value: unknown) =>
    setForm((prev) => ({ ...prev, [field]: value }));

  return (
    <section className="page">
      <header className="page-head">
        <h1>{isNew ? "New task template" : `Edit: ${form.name}`}</h1>
        <button type="button" className="button" onClick={onCancel}>
          Cancel
        </button>
      </header>

      {error && (
        <p className="tone-danger" role="alert">
          {error}
        </p>
      )}

      <label className="stacked">
        Identifier
        <input
          value={form.name}
          disabled={!isNew}
          onChange={(e) => patch("name", e.target.value)}
          placeholder="bt1"
        />
        {isNew && (
          <span className="muted small">
            Gantt templates reference this via <code>uses: bt1</code>. Cannot
            be changed after creation.
          </span>
        )}
      </label>

      <label className="stacked">
        Display name
        <input
          value={form.display_name}
          onChange={(e) => patch("display_name", e.target.value)}
          placeholder="Data backup"
        />
      </label>

      <label className="stacked">
        Default duration
        <input
          value={form.duration_default}
          onChange={(e) => patch("duration_default", e.target.value)}
          placeholder="1H"
        />
        <span className="muted small">
          Formats: 30M · 4H · 2D. <code>D</code> means one working day in
          business calendars.
        </span>
      </label>

      <label className="stacked">
        Schedule mode
        <select
          value={form.schedule_mode}
          onChange={(e) => patch("schedule_mode", e.target.value)}
        >
          <option value="continuous">Continuous 24×7</option>
          <option value="business">Business calendar</option>
        </select>
      </label>

      <label className="stacked">
        On failure
        <select
          value={form.on_failure}
          onChange={(e) => patch("on_failure", e.target.value)}
        >
          {Object.entries(FAILURE_LABELS).map(([k, v]) => (
            <option key={k} value={k}>
              {v}
            </option>
          ))}
        </select>
      </label>

      <label className="checkbox">
        <input
          type="checkbox"
          checked={form.allow_manual_override}
          onChange={(e) => patch("allow_manual_override", e.target.checked)}
        />
        Allow manual completion override
      </label>

      <hr style={{ margin: "16px 0" }} />
      <h3 style={{ margin: "0 0 8px" }}>Completion</h3>

      <label className="stacked">
        API handler
        <select
          value={form.task_api ?? ""}
          onChange={(e) => patch("task_api", e.target.value || null)}
        >
          <option value="">Manual only</option>
          {handlers.map((h) => (
            <option key={h.name} value={h.name}>
              {h.builtin ? "🔧 " : ""}
              {h.name}
              {h.description ? ` — ${h.description}` : ""}
            </option>
          ))}
        </select>
        <span className="muted small">
          Only handlers registered on the server are listed — typos cannot slip
          through until a case is running.
        </span>
      </label>

      {form.task_api && (
        <label className="stacked">
          API mode
          <select
            value={form.api_mode ?? "trigger_poll"}
            onChange={(e) => patch("api_mode", e.target.value)}
          >
            {Object.entries(API_MODE_LABELS).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
        </label>
      )}

      <div className="toolbar">
        <button
          type="button"
          className="button primary"
          disabled={saving || !form.name}
          onClick={() =>
            onSave({
              ...form,
              task_api: form.task_api || null,
              api_mode: form.task_api
                ? (form.api_mode || "trigger_poll")
                : null,
            })
          }
        >
          {saving ? "Saving…" : "Save"}
        </button>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------

export function TaskTemplateLibrary() {
  const client = useQueryClient();
  const [editing, setEditing] = useState<TaskTemplate | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const templates = useQuery({
    queryKey: ["task-templates"],
    queryFn: api.taskTemplates,
  });
  const handlers = useQuery({
    queryKey: ["handlers"],
    queryFn: api.handlers,
  });

  const canManage = me.data?.user.is_template_admin ?? false;

  const done = () => {
    setError(null);
    setEditing(null);
    setIsNew(false);
    client.invalidateQueries({ queryKey: ["task-templates"] });
  };

  const save = useMutation({
    mutationFn: (t: TaskTemplate) =>
      api.putTaskTemplate(t.name, {
        name: t.name,
        display_name: t.display_name,
        duration_default: t.duration_default,
        schedule_mode: t.schedule_mode,
        task_api: t.task_api,
        api_mode: t.api_mode,
        api_config: t.api_config,
        on_failure: t.on_failure,
        allow_manual_override: t.allow_manual_override,
      }),
    onSuccess: done,
    onError: (err: unknown) =>
      setError(err instanceof ApiError ? err.message : String(err)),
  });

  const remove = useMutation({
    mutationFn: (name: string) => api.deleteTaskTemplate(name),
    onSuccess: () => {
      setDeleteTarget(null);
      client.invalidateQueries({ queryKey: ["task-templates"] });
    },
    onError: (err: unknown) => {
      setDeleteTarget(null);
      setError(err instanceof ApiError ? err.message : String(err));
    },
  });

  if (editing) {
    return (
      <Editor
        initial={editing}
        isNew={isNew}
        handlers={handlers.data ?? []}
        onSave={(t) => save.mutate(t)}
        onCancel={() => {
          setEditing(null);
          setError(null);
        }}
        saving={save.isPending}
        error={error}
      />
    );
  }

  return (
    <section className="page">
      <header className="page-head">
        <h1>Task templates</h1>
        {canManage && (
          <button
            type="button"
            className="button primary"
            onClick={() => {
              setError(null);
              setEditing(blank());
              setIsNew(true);
            }}
          >
            + New task template
          </button>
        )}
      </header>

      {error && (
        <p className="tone-danger" role="alert">
          {error}
        </p>
      )}

      {deleteTarget && (
        <div className="modal-backdrop" role="dialog" aria-modal="true">
          <div className="modal">
            <header>
              <h2>Delete {deleteTarget}?</h2>
              <button
                type="button"
                onClick={() => setDeleteTarget(null)}
                aria-label="Close"
              >
                ✕
              </button>
            </header>
            <p>
              This cannot be undone. Any gantt template that still references{" "}
              <strong>{deleteTarget}</strong> must be updated first — the
              server will refuse if it is still in use.
            </p>
            <div className="toolbar">
              <button
                type="button"
                className="button"
                onClick={() => setDeleteTarget(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="button primary"
                style={{ background: "var(--danger)", borderColor: "var(--danger)" }}
                disabled={remove.isPending}
                onClick={() => remove.mutate(deleteTarget)}
              >
                {remove.isPending ? "Deleting…" : "Delete"}
              </button>
            </div>
          </div>
        </div>
      )}

      {templates.data?.length === 0 && (
        <div className="empty">
          <p>
            No task templates yet.{" "}
            {canManage
              ? "Create one here, or import via gantt import."
              : "A template admin needs to add some."}
          </p>
        </div>
      )}

      <table className="table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Duration</th>
            <th>Schedule</th>
            <th>Completion</th>
            <th>On failure</th>
            {canManage && <th />}
          </tr>
        </thead>
        <tbody>
          {(templates.data ?? []).map((t) => (
            <tr key={t.name}>
              <td>
                <span className="strong">{t.name}</span>
                {t.display_name && (
                  <div className="muted small">{t.display_name}</div>
                )}
              </td>
              <td className="muted">{t.duration_default}</td>
              <td className="muted">{t.schedule_mode}</td>
              <td>
                {t.task_api ? (
                  <>
                    🔌 <code>{t.task_api}</code>
                    {t.api_mode && (
                      <div className="muted small">
                        {API_MODE_LABELS[t.api_mode] ?? t.api_mode}
                      </div>
                    )}
                  </>
                ) : (
                  <span className="muted">✋ Manual</span>
                )}
              </td>
              <td className="muted small">
                {FAILURE_LABELS[t.on_failure] ?? t.on_failure}
              </td>
              {canManage && (
                <td>
                  <div className="row-actions">
                    <button
                      type="button"
                      className="button small"
                      onClick={() => {
                        setError(null);
                        setEditing(t as TaskTemplate);
                        setIsNew(false);
                      }}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      className="button small"
                      style={{ color: "var(--danger)", borderColor: "var(--danger)" }}
                      onClick={() => setDeleteTarget(t.name)}
                    >
                      Delete
                    </button>
                  </div>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

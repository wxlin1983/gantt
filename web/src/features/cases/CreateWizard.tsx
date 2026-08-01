/**
 * Case creation wizard (design.md §3).
 *
 * Four steps, and the schedule is previewed *before* anything is created. The
 * point is that nobody should have to create a case, look at it, and delete it
 * again to find out what the dates would be.
 */

import { useMutation, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError, api } from "../../api/client";
import type { Preview } from "../../api/types";
import {
  formatDate,
  formatDuration,
  formatMoment,
} from "../../lib/format";

/** End of the working day: what "finish by this date" almost always means. */
const DEFAULT_TARGET_TIME = "18:00";

/** Stable for the whole wizard, so a double submit cannot create two cases. */
function newIdempotencyKey(): string {
  return `wizard-${crypto.randomUUID()}`;
}

interface ParamDef {
  para_name?: string;
  name?: string;
  para_type?: string;
  para_default?: unknown;
  description?: string;
  choices?: unknown[];
  group?: string;
  required?: boolean;
}

interface RoleDef {
  name: string;
  display_name?: string;
  required?: boolean;
  default_group?: string;
}

export function CreateWizard() {
  const navigate = useNavigate();
  const [step, setStep] = useState(1);
  const [templateName, setTemplateName] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [params, setParams] = useState<Record<string, unknown>>({});
  const [roles, setRoles] = useState<Record<string, string>>({});
  const [targetDate, setTargetDate] = useState("");
  // Split rather than one datetime-local: picking a date in that control
  // leaves the time blank, so the value stays invalid until the user notices
  // they also have to type a time. A completion target is almost always
  // end-of-day anyway, so it is pre-filled and rarely touched.
  const [targetTime, setTargetTime] = useState(DEFAULT_TARGET_TIME);
  const [error, setError] = useState<string | null>(null);
  const [idempotencyKey] = useState(newIdempotencyKey);

  const templates = useQuery({
    queryKey: ["templates"],
    queryFn: api.templates,
  });
  const people = useQuery({ queryKey: ["users"], queryFn: api.users });
  const template = useQuery({
    queryKey: ["template", templateName],
    queryFn: () => api.template(templateName!),
    enabled: Boolean(templateName),
  });

  const definition = (template.data?.definition ?? {}) as {
    template_para?: ParamDef[];
    roles?: RoleDef[];
    buffer?: string | number;
  };
  const paramDefs = definition.template_para ?? [];
  const roleDefs = definition.roles ?? [];

  // Local wall-clock; `new Date` interprets it in the browser's zone and the
  // API is handed UTC.
  const target = targetDate ? `${targetDate}T${targetTime}` : "";

  const preview = useMutation({
    mutationFn: () =>
      api.previewCase({
        template_name: templateName,
        target_date: new Date(target).toISOString(),
        params,
        role_assignments: roles,
        name: name || "preview",
      }),
    onError: (err) => setError(describe(err)),
  });

  const create = useMutation({
    mutationFn: () =>
      api.createCase({
        name,
        template_name: templateName,
        target_date: new Date(target).toISOString(),
        params,
        role_assignments: roles,
        idempotency_key: idempotencyKey,
      }),
    onSuccess: (created) => navigate(`/cases/${created.id}`),
    onError: (err) => setError(describe(err)),
  });

  const missingRoles = roleDefs
    .filter((role) => role.required !== false && !roles[role.name])
    .map((role) => role.name);

  const canPreview =
    Boolean(templateName && name && target) && missingRoles.length === 0;

  return (
    <section className="page narrow">
      <h1>Create case</h1>
      <ol className="steps">
        {["Template", "Parameters", "Target date", "Preview"].map(
          (label, index) => (
            <li
              key={label}
              className={step === index + 1 ? "active" : step > index + 1 ? "done" : ""}
            >
              {label}
            </li>
          ),
        )}
      </ol>

      {error && (
        <div className="banner tone-danger" role="alert">
          {error}
        </div>
      )}

      {step === 1 && (
        <div className="cards">
          {(templates.data ?? []).map((item) => (
            <button
              key={item.name}
              type="button"
              className={`card ${templateName === item.name ? "selected" : ""}`}
              onClick={() => {
                setTemplateName(item.name);
                setParams({});
                setRoles({});
                setStep(2);
              }}
            >
              <strong>{item.name}</strong>
              <span className="muted small">
                v{item.version} · {item.step_count} steps
              </span>
              {item.description && <p>{item.description}</p>}
              <span className="muted small">
                {item.active_cases} active case
                {item.active_cases === 1 ? "" : "s"}
              </span>
            </button>
          ))}
          {templates.data?.length === 0 && (
            <div className="empty">
              <p>No published templates yet.</p>
            </div>
          )}
        </div>
      )}

      {step === 2 && (
        <div className="form">
          <label>
            Case name *
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="2026Q3 launch — model A"
            />
          </label>

          {paramDefs.length > 0 && <h3>Parameters</h3>}
          {paramDefs.map((param) => {
            const key = param.para_name ?? param.name ?? "";
            return (
              <label key={key}>
                {key}
                {param.description && (
                  <span className="muted small"> ⓘ {param.description}</span>
                )}
                {param.para_type === "enum" ? (
                  <select
                    value={String(params[key] ?? param.para_default ?? "")}
                    onChange={(event) =>
                      setParams({ ...params, [key]: event.target.value })
                    }
                  >
                    {(param.choices ?? []).map((choice) => (
                      <option key={String(choice)} value={String(choice)}>
                        {String(choice)}
                      </option>
                    ))}
                  </select>
                ) : param.para_type === "bool" ? (
                  <input
                    type="checkbox"
                    checked={Boolean(params[key] ?? param.para_default)}
                    onChange={(event) =>
                      setParams({ ...params, [key]: event.target.checked })
                    }
                  />
                ) : (
                  <input
                    type={
                      param.para_type === "int" || param.para_type === "float"
                        ? "number"
                        : "text"
                    }
                    value={String(params[key] ?? param.para_default ?? "")}
                    onChange={(event) =>
                      setParams({
                        ...params,
                        [key]:
                          param.para_type === "int"
                            ? Number(event.target.value)
                            : event.target.value,
                      })
                    }
                  />
                )}
                <span className="muted small">
                  default {String(param.para_default ?? "—")}
                </span>
              </label>
            );
          })}

          {/* Roles are a separate block on purpose: the template declares the
              slots, and binding real people happens here (design.md §3). */}
          {roleDefs.length > 0 && <h3>Role assignment</h3>}
          {roleDefs.map((role) => (
            <label key={role.name}>
              {role.display_name || role.name}
              {role.required !== false && " *"}
              {/* A list, not a text field. Typing a username here was how a
                  case came to be assigned to nobody: the name matched no user,
                  resolution found no one, and every task came out unowned. */}
              <select
                value={roles[role.name] ?? ""}
                onChange={(event) =>
                  setRoles({ ...roles, [role.name]: event.target.value })
                }
              >
                <option value="">Choose someone…</option>
                {(people.data ?? [])
                  .filter((person) => person.is_active)
                  .map((person) => (
                    <option key={person.id} value={person.username}>
                      {person.display_name} ({person.username})
                    </option>
                  ))}
              </select>
              {role.default_group && (
                <span className="muted small">
                  usually from {role.default_group}
                </span>
              )}
            </label>
          ))}

          <nav className="wizard-nav">
            <button type="button" className="button" onClick={() => setStep(1)}>
              Back
            </button>
            <button
              type="button"
              className="button primary"
              disabled={!name || missingRoles.length > 0}
              onClick={() => setStep(3)}
            >
              Next
            </button>
          </nav>
          {missingRoles.length > 0 && (
            <p className="muted small">
              Assign {missingRoles.join(", ")} to continue.
            </p>
          )}
        </div>
      )}

      {step === 3 && (
        <div className="form">
          <label>
            Target completion *
            <span className="field-row">
              <input
                type="date"
                value={targetDate}
                onChange={(event) => setTargetDate(event.target.value)}
              />
              <input
                type="time"
                value={targetTime}
                step={900}
                onChange={(event) =>
                  setTargetTime(event.target.value || DEFAULT_TARGET_TIME)
                }
              />
            </span>
          </label>
          <p className="muted small">
            The schedule is worked backwards from this moment.
            {definition.buffer
              ? ` This template reserves a ${definition.buffer} buffer before it.`
              : ""}
          </p>
          <nav className="wizard-nav">
            <button type="button" className="button" onClick={() => setStep(2)}>
              Back
            </button>
            <button
              type="button"
              className="button primary"
              disabled={!canPreview}
              onClick={() => {
                setError(null);
                preview.mutate();
                setStep(4);
              }}
            >
              Preview schedule
            </button>
          </nav>
        </div>
      )}

      {step === 4 && (
        <div>
          {preview.isPending && <p className="muted">Working out dates…</p>}
          {preview.data && <PreviewPanel preview={preview.data} />}
          <nav className="wizard-nav">
            <button type="button" className="button" onClick={() => setStep(3)}>
              Back
            </button>
            <button
              type="button"
              className="button primary"
              disabled={create.isPending || !preview.data}
              onClick={() => create.mutate()}
            >
              Create case
            </button>
          </nav>
        </div>
      )}
    </section>
  );
}

function PreviewPanel({ preview }: { preview: Preview }) {
  const slackHours = Math.round(preview.slack_seconds / 3600);
  const tone = preview.feasible
    ? slackHours < 24
      ? "warning"
      : "success"
    : "danger";

  const byPhase = useMemo(() => {
    const groups = new Map<string, typeof preview.tasks>();
    for (const task of preview.tasks) {
      const key = task.phase || "";
      groups.set(key, [...(groups.get(key) ?? []), task]);
    }
    return [...groups.entries()];
  }, [preview.tasks]);

  return (
    <div>
      <div className={`banner tone-${tone}`}>
        {preview.feasible ? (
          <>
            Needs {formatDuration(preview.critical_path_seconds)}; earliest
            start {formatDate(preview.earliest_start)} — {slackHours}h of slack
            from now.
          </>
        ) : (
          <>
            {/* Reported, not blocked: flows often start already behind, and
                refusing would just make people enter fake dates. */}
            The plan would have had to start {Math.abs(slackHours)}h ago. You
            can still create it; it will show as overdue.
          </>
        )}
      </div>

      {byPhase.map(([phase, tasks]) => (
        <div key={phase}>
          {phase && <h4 className="phase-head">{phase}</h4>}
          <table className="table compact">
            <tbody>
              {tasks.map((task) => (
                <tr key={task.name}>
                  <td className="strong">
                    {task.is_on_critical_path && (
                      <span className="badge badge-critical">!</span>
                    )}{" "}
                    {task.display_name || task.name}
                  </td>
                  <td className="muted">{task.owner ?? "unassigned"}</td>
                  <td>{formatDuration(task.duration_seconds)}</td>
                  <td className="muted">
                    {formatMoment(task.baseline_start)} →{" "}
                    {formatMoment(task.baseline_end)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}

      <p className="muted small">
        {preview.tasks.length} steps · critical path{" "}
        {formatDuration(preview.critical_path_seconds)}
        {preview.buffer_seconds > 0 &&
          ` · buffer ${formatDuration(preview.buffer_seconds)}`}
      </p>

      {preview.skipped_tasks.length > 0 && (
        <div className="banner">
          {/* Surfaced before creation: discovering a missing step afterwards is
              far more confusing (design.md §3 step 4). */}
          ⓘ {preview.skipped_tasks.length} step(s) skipped by these parameters:{" "}
          {preview.skipped_tasks
            .map((entry) => entry.label || entry.id)
            .join(", ")}
        </div>
      )}

      {preview.warnings.map((warning) => (
        <p key={warning.code + warning.path} className="muted small">
          ⚠ {warning.code} {warning.message}
        </p>
      ))}
    </div>
  );
}

function describe(error: unknown): string {
  if (error instanceof ApiError) {
    const issues = error.issues.map((issue) => issue.message).join("; ");
    return issues ? `${error.code}: ${issues}` : `${error.code}: ${error.message}`;
  }
  return String(error);
}

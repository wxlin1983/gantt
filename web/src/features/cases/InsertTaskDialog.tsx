/**
 * Insert a step into a running case (design.md §6).
 *
 * The wiring choice is spelled out rather than inferred, because it is the
 * thing people get wrong: "serial" cuts the link it sits between and lengthens
 * the path; "parallel" hangs alongside and does not. The dialog previews the
 * cost either way before anything is committed.
 */

import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { ApiError, api } from "../../api/client";
import type { CaseDetail, Simulation } from "../../api/types";
import { formatDelta, formatDuration } from "../../lib/format";

type Mode = "serial" | "parallel";

interface Props {
  detail: CaseDetail;
  /** Task the insertion hangs off, when opened from a specific row. */
  anchor?: string;
  onClose: () => void;
  onInserted: (next: CaseDetail) => void;
}

export function InsertTaskDialog({
  detail,
  anchor,
  onClose,
  onInserted,
}: Props) {
  const [name, setName] = useState("");
  const [label, setLabel] = useState("");
  const [hours, setHours] = useState(4);
  const [template, setTemplate] = useState("");
  const [predecessors, setPredecessors] = useState<string[]>(
    anchor ? [anchor] : [],
  );
  const [successors, setSuccessors] = useState<string[]>(() =>
    anchor
      ? detail.dependencies
          .filter((edge) => edge.predecessor === anchor)
          .map((edge) => edge.successor)
      : [],
  );
  const [mode, setMode] = useState<Mode>("serial");
  const [error, setError] = useState<string | null>(null);
  const [impact, setImpact] = useState<Simulation | null>(null);

  const templates = useQuery({
    queryKey: ["task-templates"],
    queryFn: api.taskTemplates,
  });

  // Preview the knock-on as the inputs change, so the cost is visible before
  // committing rather than discovered afterwards.
  useEffect(() => {
    if (predecessors.length === 0) {
      setImpact(null);
      return;
    }
    const timer = setTimeout(() => {
      api
        .simulate(detail.id, {
          insert_after: predecessors[0],
          insert_duration_seconds: Math.round(hours * 3600),
        })
        .then(setImpact)
        .catch(() => setImpact(null));
    }, 300);
    return () => clearTimeout(timer);
  }, [detail.id, predecessors, hours]);

  const insert = useMutation({
    mutationFn: () =>
      api.insertTask(detail.id, {
        name,
        display_name: label || name,
        task_template: template || null,
        duration_seconds: Math.round(hours * 3600),
        predecessors,
        successors: mode === "serial" ? successors : [],
        mode,
      }),
    onSuccess: (next) => {
      onInserted(next);
      onClose();
    },
    onError: (err) =>
      setError(err instanceof ApiError ? `${err.code}: ${err.message}` : String(err)),
  });

  const names = detail.tasks.map((task) => task.name);
  const valid = name.trim().length > 0 && predecessors.length > 0;

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <div className="modal">
        <header>
          <h2>Insert a step</h2>
          <button type="button" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        <div className="form">
          <label>
            Step id *
            <input
              value={name}
              placeholder="supervisor_review"
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <label>
            Label
            <input
              value={label}
              placeholder="Supervisor review"
              onChange={(event) => setLabel(event.target.value)}
            />
          </label>
          <label>
            From task template
            <select
              value={template}
              onChange={(event) => {
                setTemplate(event.target.value);
              }}
            >
              <option value="">— blank step —</option>
              {(templates.data ?? []).map((item) => (
                <option key={item.name} value={item.name}>
                  {item.display_name || item.name}
                  {item.task_api ? " 🔌" : " ✋"}
                </option>
              ))}
            </select>
            <span className="muted small">
              {/* Which steps finish themselves and which need a person is
                  worth knowing before adding one. */}
              🔌 completed by an API · ✋ completed by hand
            </span>
          </label>
          <label>
            Duration
            <span className="field-row">
              <input
                type="number"
                min={0}
                step={0.5}
                value={hours}
                onChange={(event) => setHours(Number(event.target.value))}
              />
              <span className="muted small" style={{ alignSelf: "center" }}>
                hours
              </span>
            </span>
          </label>

          <label>
            After
            <select
              multiple
              size={4}
              value={predecessors}
              onChange={(event) =>
                setPredecessors(
                  [...event.target.selectedOptions].map((o) => o.value),
                )
              }
            >
              {names.map((task) => (
                <option key={task} value={task}>
                  {task}
                </option>
              ))}
            </select>
          </label>

          <fieldset className="choice">
            <legend>Wiring</legend>
            <label className="checkbox">
              <input
                type="radio"
                checked={mode === "serial"}
                onChange={() => setMode("serial")}
              />
              Serial — cut the existing link and sit between the two steps
            </label>
            <label className="checkbox">
              <input
                type="radio"
                checked={mode === "parallel"}
                onChange={() => setMode("parallel")}
              />
              Parallel — hang alongside, leaving the existing link intact
            </label>
          </fieldset>

          {mode === "serial" && (
            <label>
              Before
              <select
                multiple
                size={4}
                value={successors}
                onChange={(event) =>
                  setSuccessors(
                    [...event.target.selectedOptions].map((o) => o.value),
                  )
                }
              >
                {names.map((task) => (
                  <option key={task} value={task}>
                    {task}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>

        {impact && (
          <div
            className={`banner ${
              impact.exceeds_target ? "tone-danger" : "tone-warning"
            }`}
          >
            {impact.delta_seconds === 0
              ? "This does not move the finish date."
              : `This pushes the case out by ${formatDuration(
                  impact.delta_seconds,
                )}` +
                (impact.exceeds_target
                  ? `, ${formatDelta(
                      impact.exceeds_target_by_seconds,
                    )} past the target.`
                  : ", still inside the target.")}
          </div>
        )}

        <p className="muted small">
          {/* Inserted steps have no baseline, and saying so up front avoids a
              "why is there no variance" question later. */}
          An inserted step has no original plan, so it is drawn as a single bar
          and reports no variance. This changes only this case, never the
          template.
        </p>

        {error && <div className="banner tone-danger">{error}</div>}

        <nav className="wizard-nav">
          <button type="button" className="button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="button primary"
            disabled={!valid || insert.isPending}
            onClick={() => {
              setError(null);
              insert.mutate();
            }}
          >
            Insert
          </button>
        </nav>
      </div>
    </div>
  );
}

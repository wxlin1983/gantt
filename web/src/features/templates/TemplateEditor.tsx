/**
 * Template editor (design.md §9).
 *
 * Two modes rather than the three the design describes: a form view and a raw
 * JSON/YAML view, sharing one in-memory model. The flow-diagram mode is not
 * built yet -- it needs a diagramming library and is the one mode whose absence
 * does not block authoring a template.
 *
 * Validation runs on a debounce against the server, because several of the
 * checks (unknown task template, unregistered handler) need data the browser
 * does not have.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";

import { ApiError, api } from "../../api/client";
import type { TemplateHealth, ValidationResult } from "../../api/types";
import { formatDuration, formatPercent } from "../../lib/format";

type Mode = "form" | "source";

export function TemplateEditor() {
  const { name } = useParams();
  const client = useQueryClient();
  const [mode, setMode] = useState<Mode>("form");
  const [source, setSource] = useState("");
  const [parseError, setParseError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const template = useQuery({
    queryKey: ["template", name],
    queryFn: () => api.template(name!),
    enabled: Boolean(name),
  });

  // The draft is what is being edited; the published version is the fallback.
  const model = useMemo(() => {
    const definition =
      template.data?.draft?.definition ?? template.data?.definition ?? {};
    return definition as Record<string, unknown>;
  }, [template.data]);

  useEffect(() => {
    setSource(JSON.stringify(model, null, 2));
  }, [model]);

  const parsed = useMemo(() => {
    if (mode === "form") return model;
    try {
      const value = JSON.parse(source) as Record<string, unknown>;
      setParseError(null);
      return value;
    } catch (error) {
      // Keep the last good model so switching modes does not lose work; the
      // editor refuses the switch instead.
      setParseError((error as Error).message);
      return null;
    }
  }, [mode, source, model]);

  const [validation, setValidation] = useState<ValidationResult | null>(null);

  useEffect(() => {
    if (!parsed) return;
    const timer = setTimeout(() => {
      api
        .validateTemplate(parsed)
        .then(setValidation)
        .catch(() => setValidation(null));
    }, 300);
    return () => clearTimeout(timer);
  }, [parsed]);

  const save = useMutation({
    mutationFn: () => api.saveDraft(name!, parsed, "edited in the editor"),
    onSuccess: () => {
      setNotice("Draft saved");
      client.invalidateQueries({ queryKey: ["template", name] });
    },
    onError: (error) => setNotice(describe(error)),
  });

  const publish = useMutation({
    mutationFn: () => api.publishTemplate(name!),
    onSuccess: (result) => {
      setNotice(`Published v${result.version}`);
      client.invalidateQueries({ queryKey: ["template", name] });
      client.invalidateQueries({ queryKey: ["templates"] });
    },
    onError: (error) => setNotice(describe(error)),
  });

  const health = useQuery({
    queryKey: ["template-health", name],
    queryFn: () => api.templateHealth(name!),
    enabled: Boolean(name),
  });

  if (template.isLoading) return <p className="page muted">Loading…</p>;

  // Published only -- a brand new draft has none, and "0 versions" beside
  // "draft v1" read as though the draft itself had not been saved.
  const published =
    template.data?.versions.filter((v) => v.status === "published").length ?? 0;
  const dynamic = hasConditionalTasks(parsed ?? model);

  return (
    <section className="page">
      <h1>{name}</h1>
      <p className="muted">
        {template.data?.draft
          ? `draft v${template.data.draft.version} (unpublished)`
          : `v${template.data?.latest_version} published`}{" "}
        · {published} published version{published === 1 ? "" : "s"}
      </p>

      <div className="toolbar">
        <div className="segmented">
          <button
            type="button"
            className={mode === "form" ? "active" : ""}
            onClick={() => setMode("form")}
            disabled={Boolean(parseError)}
            title={parseError ? "Fix the syntax error first" : undefined}
          >
            Form
          </button>
          <button
            type="button"
            className={mode === "source" ? "active" : ""}
            onClick={() => setMode("source")}
          >
            Source
          </button>
        </div>
        <button
          type="button"
          className="button"
          disabled={!parsed || save.isPending}
          onClick={() => save.mutate()}
        >
          Save draft
        </button>
        <button
          type="button"
          className="button primary"
          disabled={
            !template.data?.draft ||
            !validation?.ok ||
            publish.isPending
          }
          onClick={() => publish.mutate()}
        >
          Publish
        </button>
        <button
          type="button"
          className="button"
          onClick={() =>
            api.exportTemplate(name!).then((text) => download(name!, text))
          }
        >
          Export YAML
        </button>
      </div>

      {notice && <div className="banner">{notice}</div>}
      {parseError && (
        <div className="banner tone-danger">Syntax error: {parseError}</div>
      )}

      {dynamic && (
        <div className="banner tone-warning">
          {/* Static checks cannot settle a template whose shape depends on its
              parameters, so the editor pushes for a trial run (§4.7). */}
          This template's shape depends on its parameters. Static validation
          cannot prove the resulting graph is sound — run a trial from the
          create wizard with a real parameter set before publishing.
        </div>
      )}

      {mode === "source" ? (
        <textarea
          className="source-editor"
          spellCheck={false}
          value={source}
          onChange={(event) => setSource(event.target.value)}
          rows={28}
        />
      ) : (
        <FormView definition={model} />
      )}

      <ValidationPanel result={validation} />

      {health.data && <HealthPanel health={health.data} />}
    </section>
  );
}

function FormView({ definition }: { definition: Record<string, unknown> }) {
  const flow = flatten(definition.flow);
  return (
    <div className="form-view">
      <dl className="drawer-fields">
        <dt>Description</dt>
        <dd>{String(definition.description ?? "—")}</dd>
        <dt>Project buffer</dt>
        <dd>{String(definition.buffer ?? "none")}</dd>
        <dt>Roles</dt>
        <dd>
          {(definition.roles as { name: string }[] | undefined)
            ?.map((role) => role.name)
            .join(", ") || "none"}
        </dd>
        <dt>Parameters</dt>
        <dd>
          {(definition.template_para as { para_name?: string }[] | undefined)
            ?.map((param) => param.para_name)
            .join(", ") || "none"}
        </dd>
      </dl>

      <table className="table compact">
        <thead>
          <tr>
            <th>Step</th>
            <th>Uses</th>
            <th>Duration</th>
            <th>Depends on</th>
            <th>Flags</th>
          </tr>
        </thead>
        <tbody>
          {flow.map((node) => (
            <tr key={String(node.id)}>
              <td className="strong">
                {String(node.label ?? node.id)}
                {node.when !== undefined && (
                  <span className="badge" title={String(node.when)}>
                    ?
                  </span>
                )}
              </td>
              <td className="muted">{String(node.uses ?? "—")}</td>
              <td>{String(node.duration ?? "—")}</td>
              <td className="muted">{describeRequirement(node.requirement)}</td>
              <td className="muted small">
                {[
                  node.optional ? "optional" : "",
                  node.on_failure && node.on_failure !== "block"
                    ? String(node.on_failure)
                    : "",
                  node.phase ? String(node.phase) : "",
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small">
        The form view is read-only for now; use the source view to edit.
      </p>
    </div>
  );
}

function ValidationPanel({ result }: { result: ValidationResult | null }) {
  if (!result) return null;
  if (result.ok && result.warnings.length === 0) {
    return <p className="tone-success">✓ No problems found</p>;
  }
  return (
    <div className="validation">
      {result.errors.map((issue) => (
        <p key={issue.code + issue.path} className="tone-danger">
          ✕ <code>{issue.code}</code>{" "}
          {issue.path && <span className="muted">{issue.path}</span>}{" "}
          {issue.message}
        </p>
      ))}
      {result.warnings.map((issue) => (
        <p key={issue.code + issue.path} className="tone-warning">
          ⚠ <code>{issue.code}</code>{" "}
          {issue.path && <span className="muted">{issue.path}</span>}{" "}
          {issue.message}
        </p>
      ))}
    </div>
  );
}

function HealthPanel({ health }: { health: TemplateHealth }) {
  if (health.case_count === 0) {
    return (
      <div className="panel">
        <h3>Health</h3>
        <p className="muted">
          No completed cases yet, so there is nothing to calibrate against.
        </p>
      </div>
    );
  }
  return (
    <div className="panel">
      <h3>Health</h3>
      <p className="muted">
        {health.case_count} completed case(s) · on time{" "}
        {formatPercent(health.on_time_ratio)}
      </p>
      <table className="table compact">
        <thead>
          <tr>
            <th>Step</th>
            <th>Planned</th>
            <th>Median</th>
            <th>P80</th>
            <th>Over plan</th>
            <th>On critical path</th>
          </tr>
        </thead>
        <tbody>
          {health.tasks.map((task) => (
            <tr key={task.task_id}>
              <td className="strong">{task.label}</td>
              <td>{formatDuration(task.planned_duration_seconds)}</td>
              <td
                className={
                  task.actual_median_seconds >
                  task.planned_duration_seconds * 1.2
                    ? "tone-warning"
                    : ""
                }
              >
                {formatDuration(task.actual_median_seconds)}
              </td>
              <td>{formatDuration(task.actual_p80_seconds)}</td>
              <td>{formatPercent(task.overrun_ratio)}</td>
              <td>{formatPercent(task.on_critical_path_ratio)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {health.bottlenecks.map((entry) => (
        <p key={entry.task_id} className="tone-warning">
          🔥 {entry.task_id}: {entry.reason}
        </p>
      ))}
    </div>
  );
}

interface FlowNode {
  id?: unknown;
  label?: unknown;
  uses?: unknown;
  duration?: unknown;
  requirement?: unknown;
  when?: unknown;
  optional?: unknown;
  on_failure?: unknown;
  phase?: unknown;
}

function flatten(flow: unknown): FlowNode[] {
  if (!Array.isArray(flow)) return [];
  const out: FlowNode[] = [];
  for (const entry of flow) {
    const node = entry as FlowNode & { tasks?: unknown; phase?: unknown };
    if (Array.isArray(node.tasks)) {
      for (const task of node.tasks) {
        out.push({ ...(task as FlowNode), phase: node.phase });
      }
    } else {
      out.push(node);
    }
  }
  return out;
}

function describeRequirement(requirement: unknown): string {
  if (requirement === undefined || requirement === null) return "—";
  if (typeof requirement === "string") {
    return requirement === "none" ? "—" : requirement;
  }
  if (Array.isArray(requirement)) {
    return requirement.map((entry) => describeRequirement(entry)).join(", ");
  }
  const entry = requirement as { task?: string; lag?: unknown };
  return entry.lag ? `${entry.task} +${entry.lag}` : String(entry.task);
}

function hasConditionalTasks(definition: Record<string, unknown>): boolean {
  return flatten(definition.flow).some((node) => node.when !== undefined);
}

function download(name: string, text: string): void {
  const url = URL.createObjectURL(
    new Blob([text], { type: "application/yaml" }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = `${name}.yaml`;
  link.click();
  URL.revokeObjectURL(url);
}

function describe(error: unknown): string {
  if (error instanceof ApiError) {
    const issues = error.issues.map((issue) => issue.message).join("; ");
    return issues || error.message;
  }
  return String(error);
}

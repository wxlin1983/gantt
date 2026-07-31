/**
 * Create a template from a YAML document (design.md §9, implement.md §8.7).
 *
 * The library could only be added to from the command line: the list links to
 * templates that exist and the editor loads a name that must already exist, so
 * there was no first step. This is that step, and it takes the same document
 * `gantt import` takes -- YAML rather than the JSON the editor uses, because
 * that is what the reference and every example are written in, and the server
 * already parses it.
 *
 * It always lands as a draft. Looking before publishing is the whole point of
 * drafts, and an import is exactly when you want to look.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, api } from "../../api/client";

/** Enough of a flow to be valid and to show where each piece goes. */
const SKELETON = `gantt:
  template_name: my_flow
  description: What this flow is for
  buffer: 8H

  template_para:
    - para_name: run_hours
      para_type: int
      para_default: 6

  flow:
    - id: prepare
      label: Prepare
      duration: 4H
      requirement: none

    - id: run
      label: Run
      duration: "{{ para.run_hours }}H"
      requirement: prepare

    - id: report
      label: Report
      duration: 2H
      requirement: run

# Task templates this flow refers to with \`uses:\`. Any that do not exist yet
# are created on import; ones that already exist are left alone.
#
# task_templates:
#   - id: tt_review
#     label: Review step
#     default_duration: 8H
#     schedule_mode: business
`;

export function NewTemplate() {
  const client = useQueryClient();
  const [document, setDocument] = useState(SKELETON);

  const create = useMutation({
    mutationFn: () => api.importTemplate(document),
    onSuccess: () => client.invalidateQueries({ queryKey: ["templates"] }),
  });

  const error = create.error instanceof ApiError ? create.error : null;
  const report = create.data;

  // Deliberately not redirected on success. An import can quietly decline to
  // touch a task template that already exists under the same name, and it can
  // name a credential nobody has stored yet -- both are things to read, and
  // jumping straight to the editor would scroll them past.
  if (report) {
    return (
      <section className="page narrow">
        <p className="muted small">
          <Link to="/templates">← Templates</Link>
        </p>
        <h1>{report.template_name} created</h1>
        <div className="validation">
          <p>
            Draft v{report.draft_version}. Nothing is live until you publish
            it.
          </p>
          {report.task_templates_created.length > 0 && (
            <p>
              Created task templates:{" "}
              <code>{report.task_templates_created.join(", ")}</code>
            </p>
          )}
          {report.task_templates_differing.length > 0 && (
            <p className="tone-warning">
              Already existed with different content, and were left as they
              are:{" "}
              <code>{report.task_templates_differing.join(", ")}</code>
            </p>
          )}
          {report.missing_credentials.length > 0 && (
            <p className="tone-warning">
              Referenced credentials that are not stored yet:{" "}
              <code>{report.missing_credentials.join(", ")}</code>
            </p>
          )}
        </div>
        <div className="toolbar">
          <Link
            to={`/templates/${report.template_name}`}
            className="button primary"
          >
            Open the editor
          </Link>
          <button
            type="button"
            className="button"
            onClick={() => create.reset()}
          >
            Add another
          </button>
        </div>
      </section>
    );
  }

  return (
    <section className="page narrow">
      <p className="muted small">
        <Link to="/templates">← Templates</Link>
      </p>
      <h1>New template</h1>
      <p className="muted">
        Paste a template document. It is validated before anything is stored
        and lands as a draft, so you can review it before publishing.
      </p>

      <textarea
        className="source-editor"
        rows={26}
        spellCheck={false}
        value={document}
        onChange={(event) => setDocument(event.target.value)}
        aria-label="Template YAML"
      />

      {/* Errors come back as a list of issues with a path each, which is far
          more use than the first line of a stack: they say which node. */}
      {error && (
        <div className="validation">
          {/* The headline is only worth printing when it says something the
              issue list does not; with a single issue it is the same string
              twice. */}
          {error.issues.length !== 1 && (
            <p className="tone-danger strong">{error.message}</p>
          )}
          {error.issues.map((issue) => (
            <p key={`${issue.path}-${issue.code}`} className="tone-danger">
              {issue.path && <code>{issue.path}</code>} {issue.message}
            </p>
          ))}
        </div>
      )}

      <div className="toolbar">
        <button
          type="button"
          className="button primary"
          onClick={() => create.mutate()}
          disabled={create.isPending || document.trim() === ""}
        >
          {create.isPending ? "Creating…" : "Create draft"}
        </button>
        <Link to="/templates" className="button">
          Cancel
        </Link>
      </div>
    </section>
  );
}

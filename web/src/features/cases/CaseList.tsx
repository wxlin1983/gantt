/** Case list (design.md §8): answers "how is everything doing" at a glance. */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../../api/client";
import type { CaseHealth, CaseStatus } from "../../api/types";
import {
  HEALTH_META,
  formatDate,
  formatDelta,
  formatPercent,
} from "../../lib/format";

export function CaseList() {
  const [health, setHealth] = useState<CaseHealth | "">("");
  const [status, setStatus] = useState<CaseStatus | "">("active");
  const [query, setQuery] = useState("");

  const counts = useQuery({
    queryKey: ["case-summary"],
    queryFn: api.caseSummary,
  });
  const cases = useQuery({
    queryKey: ["cases", { health, status, query }],
    queryFn: () =>
      api.cases({
        ...(health ? { health } : {}),
        ...(status ? { status } : {}),
        ...(query ? { q: query } : {}),
      }),
  });

  return (
    <section className="page">
      <header className="page-head">
        <h1>Cases</h1>
        <Link className="button primary" to="/cases/new">
          + Create case
        </Link>
      </header>

      {/* Clickable totals: the numbers double as filters. */}
      <div className="counters">
        {(
          [
            ["on_track", "On track"],
            ["at_risk", "At risk"],
            ["overdue", "Overdue"],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            className={`counter tone-${HEALTH_META[key].tone} ${
              health === key ? "active" : ""
            }`}
            onClick={() => setHealth(health === key ? "" : key)}
          >
            <strong>{counts.data?.[key] ?? "—"}</strong>
            <span>{label}</span>
          </button>
        ))}
        <button
          type="button"
          className={`counter ${status === "completed" ? "active" : ""}`}
          onClick={() =>
            setStatus(status === "completed" ? "active" : "completed")
          }
        >
          <strong>{counts.data?.completed ?? "—"}</strong>
          <span>Completed</span>
        </button>
      </div>

      <div className="filters">
        <input
          type="search"
          value={query}
          placeholder="Search case or task name…"
          onChange={(event) => setQuery(event.target.value)}
        />
        {(health || status !== "active" || query) && (
          <button
            type="button"
            className="button"
            onClick={() => {
              setHealth("");
              setStatus("active");
              setQuery("");
            }}
          >
            Clear
          </button>
        )}
      </div>

      {cases.isLoading && <p className="muted">Loading…</p>}
      {cases.data?.length === 0 && (
        <div className="empty">
          <p>No cases match these filters.</p>
        </div>
      )}

      <table className="table">
        <thead>
          <tr>
            <th>Health</th>
            <th>Case</th>
            <th>Owner</th>
            <th>Template</th>
            <th>Progress</th>
            <th>Target</th>
            <th>Forecast</th>
          </tr>
        </thead>
        <tbody>
          {(cases.data ?? []).map((row) => (
            <tr key={row.id}>
              <td>
                {/* Named, not just coloured. A bare dot in an unlabelled
                    column carries the whole reading in its hue, which
                    design.md §13 rules out. */}
                {row.health && (
                  <span className={`health tone-${HEALTH_META[row.health].tone}`}>
                    <span aria-hidden="true">
                      {HEALTH_META[row.health].icon}
                    </span>
                    {HEALTH_META[row.health].label}
                  </span>
                )}
              </td>
              <td>
                <Link to={`/cases/${row.id}`} className="strong">
                  {row.name}
                </Link>
                {/* "Stuck where, and on whom" belongs in the list: it is the
                    question the owner actually opens the case to answer. */}
                <div className="muted small">
                  {row.blocked_on.length > 0
                    ? `waiting on ${row.blocked_on.join(", ")}`
                    : row.status}
                </div>
              </td>
              {/* "Who do I chase" is the other half of "stuck where", and it
                  was only answerable by opening the case. */}
              <td className="muted">{row.owner_name || "unassigned"}</td>
              <td className="muted">
                {row.template_name} v{row.template_version}
              </td>
              <td>
                <div className="bar-track">
                  <div
                    className="bar-fill"
                    style={{
                      width: `${(row.progress_ratio ?? 0) * 100}%`,
                    }}
                  />
                </div>
                <span className="muted small">
                  {formatPercent(row.progress_ratio)}
                </span>
              </td>
              <td>{formatDate(row.target_date)}</td>
              <td
                className={
                  row.exceeds_target_by_seconds > 0 ? "tone-danger" : ""
                }
                title={row.forecast_end ?? ""}
              >
                {/* Relative variance scans faster than an absolute date; the
                    exact value is in the tooltip. */}
                {formatDelta(row.exceeds_target_by_seconds)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

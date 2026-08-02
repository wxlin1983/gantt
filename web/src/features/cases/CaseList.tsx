/**
 * Case list (design.md §8): answers "how is everything doing" at a glance.
 *
 * Filters: health counters (always visible), plus dropdowns for template,
 * owner, group, and date range. All active filters are reflected in the
 * Excel export so the download always matches what you see.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, api } from "../../api/client";
import type { CaseHealth, CaseStatus } from "../../api/types";
import {
  HEALTH_META,
  formatDate,
  formatDelta,
  formatPercent,
} from "../../lib/format";

// ---------------------------------------------------------------------------
// Filter state

interface Filters {
  health: CaseHealth | "";
  status: CaseStatus | "";
  template: string;
  ownerId: string;
  groupId: string;
  targetAfter: string;
  targetBefore: string;
  query: string;
  includeArchived: boolean;
}

const DEFAULT_FILTERS: Filters = {
  health: "",
  status: "active",
  template: "",
  ownerId: "",
  groupId: "",
  targetAfter: "",
  targetBefore: "",
  query: "",
  includeArchived: false,
};

function filtersToParams(f: Filters): Record<string, string> {
  const p: Record<string, string> = {};
  if (f.health) p.health = f.health;
  if (f.status) p.status = f.status;
  if (f.template) p.template = f.template;
  if (f.ownerId) p.owner_id = f.ownerId;
  if (f.groupId) p.group_id = f.groupId;
  if (f.targetAfter) p.target_after = new Date(f.targetAfter).toISOString();
  if (f.targetBefore) p.target_before = new Date(f.targetBefore).toISOString();
  if (f.query) p.q = f.query;
  if (f.includeArchived) p.include_archived = "true";
  return p;
}

const isDefault = (f: Filters) =>
  f.health === DEFAULT_FILTERS.health &&
  f.status === DEFAULT_FILTERS.status &&
  !f.template &&
  !f.ownerId &&
  !f.groupId &&
  !f.targetAfter &&
  !f.targetBefore &&
  !f.query &&
  !f.includeArchived;

// ---------------------------------------------------------------------------
// Row actions menu

function RowMenu({
  caseId,
  caseStatus,
  onDone,
}: {
  caseId: number;
  caseStatus: string;
  onDone: () => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const cancel = useMutation({
    mutationFn: () => api.cancelCase(caseId),
    onSuccess: () => { setOpen(false); onDone(); },
  });
  const archive = useMutation({
    mutationFn: () => api.archiveCase(caseId),
    onSuccess: () => { setOpen(false); onDone(); },
  });

  const canCancel = caseStatus === "active";
  const canArchive =
    caseStatus === "completed" || caseStatus === "cancelled";

  if (!canCancel && !canArchive) return null;

  return (
    <div ref={ref} style={{ position: "relative", display: "inline-block" }}>
      <button
        type="button"
        className="button small"
        aria-label="Case actions"
        onClick={() => setOpen((v) => !v)}
      >
        ⋯
      </button>
      {open && (
        <div
          style={{
            position: "absolute",
            right: 0,
            top: "100%",
            zIndex: 20,
            background: "var(--surface)",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius)",
            minWidth: 130,
            boxShadow: "0 4px 12px rgb(0 0 0 / 15%)",
          }}
        >
          {canCancel && (
            <button
              type="button"
              style={{
                display: "block",
                width: "100%",
                padding: "8px 12px",
                textAlign: "left",
                background: "none",
                border: 0,
                cursor: "pointer",
                font: "inherit",
                color: "var(--danger)",
              }}
              disabled={cancel.isPending}
              onClick={() => cancel.mutate()}
            >
              Cancel case
            </button>
          )}
          {canArchive && (
            <button
              type="button"
              style={{
                display: "block",
                width: "100%",
                padding: "8px 12px",
                textAlign: "left",
                background: "none",
                border: 0,
                cursor: "pointer",
                font: "inherit",
              }}
              disabled={archive.isPending}
              onClick={() => archive.mutate()}
            >
              Archive
            </button>
          )}
          <button
            type="button"
            style={{
              display: "block",
              width: "100%",
              padding: "8px 12px",
              textAlign: "left",
              background: "none",
              border: 0,
              cursor: "pointer",
              font: "inherit",
              color: "var(--muted)",
            }}
            onClick={() => setOpen(false)}
          >
            Dismiss
          </button>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------

export function CaseList() {
  const client = useQueryClient();
  const [filters, setFilters] = useState<Filters>(DEFAULT_FILTERS);
  const [downloading, setDownloading] = useState(false);
  const [dlError, setDlError] = useState<string | null>(null);

  const patch = (update: Partial<Filters>) =>
    setFilters((prev) => ({ ...prev, ...update }));

  const params = filtersToParams(filters);

  const counts = useQuery({
    queryKey: ["case-summary"],
    queryFn: api.caseSummary,
  });
  const cases = useQuery({
    queryKey: ["cases", params],
    queryFn: () => api.cases(params),
  });
  const templates = useQuery({
    queryKey: ["templates"],
    queryFn: api.templates,
  });
  const people = useQuery({ queryKey: ["users"], queryFn: api.users });
  const groups = useQuery({ queryKey: ["groups"], queryFn: api.groups });

  const invalidate = () => {
    client.invalidateQueries({ queryKey: ["cases"] });
    client.invalidateQueries({ queryKey: ["case-summary"] });
  };

  const handleExport = async () => {
    setDownloading(true);
    setDlError(null);
    try {
      const resp = await api.exportCases(params);
      if (!resp.ok) {
        setDlError(`Export failed: ${resp.statusText}`);
        return;
      }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      const ts = new Date().toISOString().slice(0, 10);
      a.download = `gantt-cases-${ts}.xlsx`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setDlError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setDownloading(false);
    }
  };

  const dirty = !isDefault(filters);

  return (
    <section className="page">
      <header className="page-head">
        <h1>Cases</h1>
        <div className="row-actions">
          <button
            type="button"
            className="button"
            disabled={downloading}
            onClick={handleExport}
            title="Export to Excel (respects current filters)"
          >
            {downloading ? "Exporting…" : "⬇ Excel"}
          </button>
          <Link className="button primary" to="/cases/new">
            + Create case
          </Link>
        </div>
      </header>

      {dlError && (
        <p className="tone-danger" role="alert">
          {dlError}
        </p>
      )}

      {/* Clickable totals double as health filters. */}
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
              filters.health === key ? "active" : ""
            }`}
            onClick={() =>
              patch({ health: filters.health === key ? "" : key })
            }
          >
            <strong>{counts.data?.[key] ?? "—"}</strong>
            <span>{label}</span>
          </button>
        ))}
        <button
          type="button"
          className={`counter ${filters.status === "completed" ? "active" : ""}`}
          onClick={() =>
            patch({
              status: filters.status === "completed" ? "active" : "completed",
            })
          }
        >
          <strong>{counts.data?.completed ?? "—"}</strong>
          <span>Completed</span>
        </button>
        <button
          type="button"
          className={`counter ${filters.status === "cancelled" ? "active" : ""}`}
          onClick={() =>
            patch({
              status: filters.status === "cancelled" ? "active" : "cancelled",
            })
          }
        >
          <strong>{counts.data?.cancelled ?? "—"}</strong>
          <span>Cancelled</span>
        </button>
      </div>

      {/* Filter bar */}
      <div className="filters">
        <input
          type="search"
          value={filters.query}
          placeholder="Search cases or tasks…"
          onChange={(e) => patch({ query: e.target.value })}
          style={{ minWidth: 180 }}
        />

        <select
          value={filters.template}
          onChange={(e) => patch({ template: e.target.value })}
          title="Filter by template"
        >
          <option value="">All templates</option>
          {(templates.data ?? []).map((t) => (
            <option key={t.name} value={t.name}>
              {t.name}
            </option>
          ))}
        </select>

        <select
          value={filters.ownerId}
          onChange={(e) => patch({ ownerId: e.target.value })}
          title="Filter by case owner"
        >
          <option value="">All owners</option>
          {(people.data ?? []).map((u) => (
            <option key={u.id} value={String(u.id)}>
              {u.display_name || u.username}
            </option>
          ))}
        </select>

        <select
          value={filters.groupId}
          onChange={(e) => patch({ groupId: e.target.value })}
          title="Filter by group"
        >
          <option value="">All groups</option>
          {(groups.data ?? []).map((g) => (
            <option key={g.id} value={String(g.id)}>
              {g.display_name || g.name}
            </option>
          ))}
        </select>

        <label style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 13 }}>
          Target from
          <input
            type="date"
            value={filters.targetAfter}
            onChange={(e) => patch({ targetAfter: e.target.value })}
            style={{ width: 130 }}
          />
        </label>

        <label style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 13 }}>
          to
          <input
            type="date"
            value={filters.targetBefore}
            onChange={(e) => patch({ targetBefore: e.target.value })}
            style={{ width: 130 }}
          />
        </label>

        <label className="checkbox" style={{ fontSize: 13 }}>
          <input
            type="checkbox"
            checked={filters.includeArchived}
            onChange={(e) => patch({ includeArchived: e.target.checked })}
          />
          Archived
        </label>

        {dirty && (
          <button
            type="button"
            className="button"
            onClick={() => setFilters(DEFAULT_FILTERS)}
          >
            Clear
          </button>
        )}
      </div>

      {cases.isLoading && <p className="muted">Loading…</p>}
      {!cases.isLoading && cases.data?.length === 0 && (
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
            <th />
          </tr>
        </thead>
        <tbody>
          {(cases.data ?? []).map((row) => (
            <tr key={row.id}>
              <td>
                {row.health && (
                  <span
                    className={`health tone-${HEALTH_META[row.health].tone}`}
                  >
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
                <div className="muted small">
                  {row.blocked_on.length > 0
                    ? `waiting on ${row.blocked_on.join(", ")}`
                    : row.status}
                </div>
              </td>
              <td className="muted">{row.owner_name || "unassigned"}</td>
              <td className="muted">
                {row.template_name} v{row.template_version}
              </td>
              <td>
                <div className="bar-track">
                  <div
                    className="bar-fill"
                    style={{ width: `${(row.progress_ratio ?? 0) * 100}%` }}
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
                {formatDelta(row.exceeds_target_by_seconds)}
              </td>
              <td>
                <RowMenu
                  caseId={row.id}
                  caseStatus={row.status}
                  onDone={invalidate}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

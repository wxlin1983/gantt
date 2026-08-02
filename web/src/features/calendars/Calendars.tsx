/**
 * Working-time calendars (design.md §9c, implement.md §3.2).
 *
 * Nothing could edit these before: no API, no CLI, no import path. The table
 * shipped with `taiwan_office` holding an empty holiday list, so every
 * business-mode task scheduled straight through every public holiday -- the
 * arithmetic exact and its input empty.
 *
 * Readable by anyone signed in, editable by administrators.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, api } from "../../api/client";
import type { CalendarDetail } from "../../api/types";
import { formatSpan } from "../../lib/format";

const WEEK = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;

/** A day's windows as `09:00-18:00, 19:00-21:00`, or a dash when closed. */
function summarise(windows: string[][] | undefined): string {
  if (!windows || windows.length === 0) return "—";
  return windows.map(([from, to]) => `${from}-${to}`).join(", ");
}

export function Calendars() {
  const client = useQueryClient();
  const [editing, setEditing] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const calendars = useQuery({
    queryKey: ["calendars"],
    queryFn: api.calendars,
  });

  const canManage = me.data?.user.is_template_admin ?? false;
  const onError = (err: unknown) =>
    setError(err instanceof ApiError ? err.message : String(err));
  const done = () => {
    setError(null);
    setEditing(null);
    client.invalidateQueries({ queryKey: ["calendars"] });
  };

  const remove = useMutation({
    mutationFn: (id: number) => api.deleteCalendar(id),
    onSuccess: done,
    onError,
  });

  return (
    <section className="page">
      <header className="page-head">
        <h1>Calendars</h1>
      </header>

      {/* The single most misread thing about this page. Without it an
          administrator adds a holiday, sees every existing date stay put, and
          concludes the feature is broken. */}
      {/* One element, not bare text plus a <strong>: `.banner` is a flex
          row, so each text node would become its own item and the sentence
          would be spread across the width. */}
      <div className="banner">
        <span>
          ⓘ Changes here apply to cases created{" "}
          <strong>afterwards</strong>. Every case freezes its calendars when it
          is created and reschedules against that copy, so dates already agreed
          with somebody never move underneath them.
        </span>
      </div>

      {error && (
        <p className="tone-danger" role="alert">
          {error}
        </p>
      )}

      <table className="table">
        <thead>
          <tr>
            <th>Calendar</th>
            <th>Timezone</th>
            <th>Working week</th>
            <th>Holidays</th>
            <th>One day is</th>
            {canManage && <th />}
          </tr>
        </thead>
        <tbody>
          {(calendars.data ?? []).map((calendar) => (
            <tr key={calendar.id}>
              <td>
                <span className="strong">{calendar.name}</span>
                {calendar.is_builtin && <span className="pill">built in</span>}
                {!calendar.is_editable && (
                  <div className="muted small">
                    24×7. Fixed — the engine uses this meaning directly.
                  </div>
                )}
              </td>
              <td className="muted">{calendar.timezone}</td>
              <td className="muted small">
                {calendar.is_editable
                  ? WEEK.filter(
                      (day) => (calendar.working_hours[day] ?? []).length > 0,
                    )
                      .map(
                        (day) =>
                          `${day} ${summarise(calendar.working_hours[day])}`,
                      )
                      .join(" · ") || "no working time"
                  : "always open"}
              </td>
              <td className="muted">
                {calendar.holidays.length === 0
                  ? "none"
                  : `${calendar.holidays.length} day${
                      calendar.holidays.length === 1 ? "" : "s"
                    }`}
              </td>
              {/* What `1D` converts to. Worth showing: that conversion has
                  been a real source of error (implement.md §4.5). */}
              <td className="muted">{formatSpan(calendar.day_seconds)}</td>
              {canManage && (
                <td>
                  <div className="row-actions">
                    <button
                      type="button"
                      className="button small"
                      disabled={!calendar.is_editable}
                      onClick={() => setEditing(calendar.id)}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      className="button small"
                      disabled={calendar.is_builtin}
                      onClick={() => {
                        if (window.confirm(`Delete ${calendar.name}?`))
                          remove.mutate(calendar.id);
                      }}
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

      {canManage && <NewCalendar onDone={done} onError={onError} />}

      {editing !== null && (
        <EditDialog
          calendar={(calendars.data ?? []).find((c) => c.id === editing)!}
          onClose={() => setEditing(null)}
          onDone={done}
          onError={onError}
        />
      )}
    </section>
  );
}

function EditDialog({
  calendar,
  onClose,
  onDone,
  onError,
}: {
  calendar: CalendarDetail;
  onClose: () => void;
  onDone: () => void;
  onError: (err: unknown) => void;
}) {
  const [timezone, setTimezone] = useState(calendar.timezone);
  const [hours, setHours] = useState<Record<string, string[][]>>(() =>
    Object.fromEntries(
      WEEK.map((day) => [day, calendar.working_hours[day] ?? []]),
    ),
  );
  // A textarea, not a date picker per entry: a year of public holidays is
  // fifteen-odd dates and pasting them is the actual task.
  const [holidays, setHolidays] = useState(calendar.holidays.join("\n"));

  const save = useMutation({
    mutationFn: () =>
      api.updateCalendar(calendar.id, {
        timezone,
        working_hours: hours,
        holidays: holidays
          .split(/[\n,]/)
          .map((entry) => entry.trim())
          .filter(Boolean),
      }),
    onSuccess: onDone,
    onError,
  });

  const windowFor = (day: string): [string, string] => {
    const existing = hours[day]?.[0];
    return [existing?.[0] ?? "09:00", existing?.[1] ?? "18:00"];
  };

  const setWindow = (day: string, index: 0 | 1, value: string) => {
    const [from, to] = windowFor(day);
    setHours({
      ...hours,
      [day]: [index === 0 ? [value, to] : [from, value]],
    });
  };

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <div className="modal">
        <header>
          <h2>{calendar.name}</h2>
          <button type="button" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        <label className="stacked">
          Timezone
          <input
            value={timezone}
            onChange={(event) => setTimezone(event.target.value)}
            placeholder="Asia/Taipei"
          />
        </label>

        <h4>Working hours</h4>
        <table className="table compact">
          <tbody>
            {WEEK.map((day) => {
              const open = (hours[day] ?? []).length > 0;
              const window = windowFor(day);
              return (
                <tr key={day}>
                  <td>
                    <label className="checkbox">
                      <input
                        type="checkbox"
                        checked={open}
                        onChange={(event) =>
                          setHours({
                            ...hours,
                            [day]: event.target.checked ? [window] : [],
                          })
                        }
                      />
                      {day}
                    </label>
                  </td>
                  <td>
                    <input
                      type="time"
                      value={window[0]}
                      disabled={!open}
                      onChange={(event) =>
                        setWindow(day, 0, event.target.value)
                      }
                    />
                    {" – "}
                    <input
                      type="time"
                      value={window[1]}
                      disabled={!open}
                      onChange={(event) =>
                        setWindow(day, 1, event.target.value)
                      }
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>

        <label className="stacked">
          Holidays — one date per line, <code>YYYY-MM-DD</code>
          <textarea
            className="source-editor"
            rows={8}
            spellCheck={false}
            value={holidays}
            onChange={(event) => setHolidays(event.target.value)}
          />
        </label>

        <div className="toolbar">
          <button
            type="button"
            className="button primary"
            disabled={save.isPending}
            onClick={() => save.mutate()}
          >
            Save calendar
          </button>
          <button type="button" className="button" onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

function NewCalendar({
  onDone,
  onError,
}: {
  onDone: () => void;
  onError: (err: unknown) => void;
}) {
  const [name, setName] = useState("");
  const [timezone, setTimezone] = useState("Asia/Taipei");

  const create = useMutation({
    mutationFn: () =>
      api.createCalendar({
        name,
        timezone,
        // A conventional week to start from; the edit dialog is where it gets
        // shaped. Starting empty would mean a calendar with no working time,
        // which cannot schedule anything.
        working_hours: Object.fromEntries(
          WEEK.map((day) => [
            day,
            day === "sat" || day === "sun" ? [] : [["09:00", "18:00"]],
          ]),
        ),
      }),
    onSuccess: () => {
      setName("");
      onDone();
    },
    onError,
  });

  return (
    <div className="inline-form">
      <input
        value={name}
        placeholder="name * (as templates refer to it)"
        onChange={(event) => setName(event.target.value)}
      />
      <input
        value={timezone}
        placeholder="Asia/Taipei"
        onChange={(event) => setTimezone(event.target.value)}
      />
      <button
        type="button"
        className="button primary"
        disabled={!name.trim() || create.isPending}
        onClick={() => create.mutate()}
      >
        Add calendar
      </button>
    </div>
  );
}

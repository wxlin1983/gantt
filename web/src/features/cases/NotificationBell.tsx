/** Notification bell (design.md §11). */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "../../api/client";
import { formatMoment } from "../../lib/format";

/** Types that mean "something needs you", as opposed to a status update. */
const URGENT = new Set([
  "task.late_start",
  "task.overdue",
  "task.failed",
  "case.overdue",
]);

export function NotificationBell() {
  const [open, setOpen] = useState(false);
  const client = useQueryClient();
  const navigate = useNavigate();

  const notifications = useQuery({
    queryKey: ["notifications"],
    queryFn: () => api.notifications(),
    // The worker's scans run every five minutes, so polling faster than that
    // only adds load without adding news.
    refetchInterval: 60_000,
  });

  const unread = (notifications.data ?? []).filter(
    (item) => item.read_at === null,
  );

  const markRead = useMutation({
    mutationFn: (id: number) => api.markRead(id),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: ["notifications"] }),
  });
  const markAll = useMutation({
    mutationFn: () => api.markAllRead(),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: ["notifications"] }),
  });

  return (
    <div className="bell-wrap">
      <button
        type="button"
        className="button small"
        onClick={() => setOpen(!open)}
        aria-label={`Notifications${unread.length ? `, ${unread.length} unread` : ""}`}
      >
        🔔
        {unread.length > 0 && <span className="bell-dot">{unread.length}</span>}
      </button>

      {open && (
        <div className="bell-panel">
          <header>
            <strong>Notifications</strong>
            {unread.length > 0 && (
              <button
                type="button"
                className="button small"
                onClick={() => markAll.mutate()}
              >
                Mark all read
              </button>
            )}
          </header>

          {(notifications.data ?? []).length === 0 && (
            <p className="muted small">Nothing yet.</p>
          )}

          <ul>
            {(notifications.data ?? []).slice(0, 20).map((item) => (
              <li
                key={item.id}
                className={[
                  item.read_at ? "read" : "unread",
                  URGENT.has(item.type) ? "urgent" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
              >
                <button
                  type="button"
                  onClick={() => {
                    markRead.mutate(item.id);
                    if (item.case_id) {
                      navigate(`/cases/${item.case_id}`);
                      setOpen(false);
                    }
                  }}
                >
                  <strong>{item.title}</strong>
                  {item.body && <span className="muted small">{item.body}</span>}
                  <span className="muted small">
                    {formatMoment(item.created_at)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

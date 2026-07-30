/** Personal queue (design.md §10): late first, then actionable. */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../../api/client";
import { STATUS_META, formatMoment } from "../../lib/format";

export function MyTasks() {
  const [includeGroup, setIncludeGroup] = useState(false);
  const tasks = useQuery({
    queryKey: ["my-tasks", includeGroup],
    queryFn: () => api.myTasks(includeGroup),
  });

  return (
    <section className="page">
      <header className="page-head">
        <h1>My tasks</h1>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={includeGroup}
            onChange={(event) => setIncludeGroup(event.target.checked)}
          />
          {/* Group members can act for each other, so their queue is offered
              too rather than hidden. */}
          Include my groups
        </label>
      </header>

      {tasks.data?.length === 0 && (
        <div className="empty">
          <p>✓ Nothing waiting on you.</p>
        </div>
      )}

      <ul className="task-feed">
        {(tasks.data ?? []).map((task) => (
          <li
            key={task.task_id}
            className={task.is_late_start ? "late" : ""}
          >
            <span className={`status status-${task.status}`}>
              {STATUS_META[task.status].icon}
            </span>
            <div>
              <Link to={`/cases/${task.case_id}`} className="strong">
                {task.display_name || task.name}
              </Link>
              <div className="muted small">
                {task.case_name} · due {formatMoment(task.baseline_end)}
                {task.is_late_start && (
                  <span className="pill tone-warning">
                    should have started
                  </span>
                )}
              </div>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

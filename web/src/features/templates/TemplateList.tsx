/** Template library. */

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { api } from "../../api/client";

export function TemplateList() {
  const templates = useQuery({
    queryKey: ["templates"],
    queryFn: api.templates,
  });

  return (
    <section className="page">
      <h1>Templates</h1>
      {templates.data?.length === 0 && (
        <div className="empty">
          <p>No templates yet. Import one or create a draft via the API.</p>
        </div>
      )}
      <table className="table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Version</th>
            <th>Steps</th>
            <th>Active cases</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {(templates.data ?? []).map((item) => (
            <tr key={item.name}>
              <td>
                <Link to={`/templates/${item.name}`} className="strong">
                  {item.name}
                </Link>
                {item.description && (
                  <div className="muted small">{item.description}</div>
                )}
              </td>
              <td>v{item.version}</td>
              <td>{item.step_count}</td>
              <td>{item.active_cases}</td>
              <td>
                {item.has_draft && <span className="pill">draft</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

/** Application shell and routes. */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";

import { ApiError, api } from "./api/client";
import { Login } from "./features/auth/Login";
import { CaseDetailPage } from "./features/cases/CaseDetail";
import { CaseList } from "./features/cases/CaseList";
import { CreateWizard } from "./features/cases/CreateWizard";
import { MyTasks } from "./features/cases/MyTasks";
import { NotificationBell } from "./features/cases/NotificationBell";
import { TemplateEditor } from "./features/templates/TemplateEditor";
import { TemplateList } from "./features/templates/TemplateList";

export function App() {
  const client = useQueryClient();
  const me = useQuery({
    queryKey: ["me"],
    queryFn: api.me,
    // A 401 is the expected answer when nobody is signed in, so it must not
    // be retried into a spinner that never resolves.
    retry: false,
  });

  if (me.isLoading) return <p className="page muted">Loading…</p>;

  const unauthenticated =
    me.error instanceof ApiError && me.error.status === 401;
  if (unauthenticated || !me.data) {
    return <Login onSignedIn={() => client.invalidateQueries()} />;
  }

  return (
    <div className="shell">
      <nav className="topbar">
        <span className="brand">Gantt</span>
        <NavLink to="/cases">Cases</NavLink>
        <NavLink to="/my-tasks">My tasks</NavLink>
        <NavLink to="/templates">Templates</NavLink>
        <span className="spacer" />
        <NotificationBell />
        <span className="muted small">{me.data.user.display_name}</span>
        <button
          type="button"
          className="button small"
          onClick={() => api.logout().then(() => client.invalidateQueries())}
        >
          Sign out
        </button>
      </nav>

      <main>
        <Routes>
          <Route path="/" element={<Navigate to="/cases" replace />} />
          <Route path="/cases" element={<CaseList />} />
          <Route path="/cases/new" element={<CreateWizard />} />
          <Route path="/cases/:id" element={<CaseDetailPage />} />
          <Route path="/my-tasks" element={<MyTasks />} />
          <Route path="/templates" element={<TemplateList />} />
          <Route path="/templates/:name" element={<TemplateEditor />} />
          <Route path="*" element={<p className="page">Not found</p>} />
        </Routes>
      </main>
    </div>
  );
}

/**
 * Typed fetch wrapper.
 *
 * The API returns one error envelope for every failure, so a single class can
 * carry the domain code through to the UI. Components branch on `code` rather
 * than parsing messages.
 */

import type {
  ApiErrorBody,
  AuditEvent,
  CaseDetail,
  CalendarDetail,
  CaseSummary,
  GroupDetail,
  HealthCounts,
  Me,
  MyTask,
  Notification,
  Person,
  Preview,
  Simulation,
  TaskRun,
  TemplateDetail,
  TemplateHealth,
  TemplateListItem,
  ValidationResult,
} from "./types";

const BASE = "/api/v1";

export class ApiError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly status: number,
    readonly details: Record<string, unknown> = {},
  ) {
    super(message);
  }

  /** Issue list from a template validation or expansion failure. */
  get issues(): { code: string; message: string; path: string }[] {
    const raw = this.details.issues;
    return Array.isArray(raw) ? raw : [];
  }
}

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    // The session lives in a cookie, so credentials must ride along even on
    // same-origin requests once a proxy is involved.
    credentials: "same-origin",
    headers:
      init.body === undefined
        ? undefined
        : { "content-type": "application/json" },
    ...init,
  });

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  const payload = text ? JSON.parse(text) : null;

  if (!response.ok) {
    const body = payload as ApiErrorBody | null;
    throw new ApiError(
      body?.error?.code ?? "E_UNKNOWN",
      body?.error?.message ?? response.statusText,
      response.status,
      body?.error?.details ?? {},
    );
  }
  return payload as T;
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  body: JSON.stringify(body),
});

export const api = {
  // auth
  login: (username: string, password: string) =>
    request<Me>("/auth/login", json({ username, password })),
  logout: () => request<void>("/auth/logout", { method: "POST" }),
  me: () => request<Me>("/auth/me"),

  // cases
  cases: (params: Record<string, string> = {}) =>
    request<CaseSummary[]>(
      `/cases?${new URLSearchParams(params).toString()}`,
    ),
  caseSummary: () => request<HealthCounts>("/cases/summary"),
  caseDetail: (id: number) => request<CaseDetail>(`/cases/${id}`),
  createCase: (body: unknown) => request<CaseDetail>("/cases", json(body)),
  previewCase: (body: unknown) =>
    request<Preview>("/cases/preview", json(body)),
  updateCase: (id: number, body: unknown) =>
    request<CaseDetail>(`/cases/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  cancelCase: (id: number, note = "") =>
    request<CaseDetail>(`/cases/${id}/cancel`, json({ note })),
  updateTask: (caseId: number, taskId: number, body: unknown) =>
    request<CaseDetail>(`/cases/${caseId}/tasks/${taskId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  completeTask: (caseId: number, taskId: number, body: unknown = {}) =>
    request<CaseDetail>(
      `/cases/${caseId}/tasks/${taskId}/complete`,
      json(body),
    ),
  insertTask: (caseId: number, body: unknown) =>
    request<CaseDetail>(`/cases/${caseId}/tasks/insert`, json(body)),
  deleteTask: (caseId: number, taskId: number, mode = "reconnect") =>
    request<CaseDetail>(
      `/cases/${caseId}/tasks/${taskId}/delete`,
      json({ mode }),
    ),
  reopenTask: (caseId: number, taskId: number) =>
    request<CaseDetail>(`/cases/${caseId}/tasks/${taskId}/reopen`, json({})),
  retryTask: (caseId: number, taskId: number) =>
    request<CaseDetail>(`/cases/${caseId}/tasks/${taskId}/retry`, json({})),
  taskRuns: (caseId: number, taskId: number) =>
    request<TaskRun[]>(`/cases/${caseId}/tasks/${taskId}/runs`),
  simulate: (caseId: number, body: unknown) =>
    request<Simulation>(`/cases/${caseId}/tasks/simulate`, json(body)),
  resetBaseline: (caseId: number, note = "") =>
    request<CaseDetail>(`/cases/${caseId}/reset-baseline`, json({ note })),
  archiveCase: (caseId: number) =>
    request<CaseDetail>(`/cases/${caseId}/archive`, json({})),
  caseAudit: (caseId: number) =>
    request<AuditEvent[]>(`/cases/${caseId}/audit`),

  // notifications
  notifications: (unreadOnly = false) =>
    request<Notification[]>(`/notifications?unread_only=${unreadOnly}`),
  markRead: (id: number) =>
    request<void>(`/notifications/${id}/read`, { method: "POST" }),
  markAllRead: () =>
    request<void>("/notifications/read-all", { method: "POST" }),

  // personal
  myTasks: (includeGroup = false) =>
    request<MyTask[]>(`/my/tasks?include_group=${includeGroup}`),

  // templates
  templates: () => request<TemplateListItem[]>("/templates"),
  template: (name: string) => request<TemplateDetail>(`/templates/${name}`),
  validateTemplate: (definition: unknown) =>
    request<ValidationResult>("/templates/validate", json({ definition })),
  saveDraft: (name: string, definition: unknown, changeNote = "") =>
    request<{ name: string; version: number }>(`/templates/${name}/draft`, {
      method: "PUT",
      body: JSON.stringify({ definition, change_note: changeNote }),
    }),
  discardDraft: (name: string) =>
    request<void>(`/templates/${name}/draft`, { method: "DELETE" }),
  publishTemplate: (name: string, changeNote = "") =>
    request<{ name: string; version: number }>(
      `/templates/${name}/publish`,
      json({ change_note: changeNote }),
    ),
  templateHealth: (name: string) =>
    request<TemplateHealth>(`/templates/${name}/health`),
  exportTemplate: (name: string) =>
    fetch(`${BASE}/templates/${name}/export`, {
      credentials: "same-origin",
    }).then((response) => response.text()),
  importTemplate: (document: string) =>
    request<{
      template_name: string;
      draft_version: number;
      task_templates_created: string[];
      /** Named in the document but already present with different content;
       *  the existing one wins and is left untouched. */
      task_templates_differing: string[];
      missing_credentials: string[];
    }>("/templates/import", json({ document })),
  // directory
  users: () => request<Person[]>("/users"),
  createUser: (body: Record<string, unknown>) =>
    request<Person>("/users", json(body)),
  updateUser: (id: number, body: Record<string, unknown>) =>
    request<Person>(`/users/${id}`, { ...json(body), method: "PATCH" }),
  setUserPassword: (id: number, password: string) =>
    request<void>(`/users/${id}/password`, {
      ...json({ password }),
      method: "PUT",
    }),
  groups: () => request<GroupDetail[]>("/groups"),
  createGroup: (body: Record<string, unknown>) =>
    request<GroupDetail>("/groups", json(body)),
  updateGroup: (id: number, body: Record<string, unknown>) =>
    request<GroupDetail>(`/groups/${id}`, { ...json(body), method: "PATCH" }),
  setGroupMembers: (
    id: number,
    members: { user_id: number; is_lead: boolean }[],
  ) =>
    request<GroupDetail>(`/groups/${id}/members`, {
      ...json({ members }),
      method: "PUT",
    }),
  deleteGroup: (id: number) =>
    request<void>(`/groups/${id}`, { method: "DELETE" }),

  // calendars
  calendars: () => request<CalendarDetail[]>("/calendars"),
  createCalendar: (body: Record<string, unknown>) =>
    request<CalendarDetail>("/calendars", json(body)),
  updateCalendar: (id: number, body: Record<string, unknown>) =>
    request<CalendarDetail>(`/calendars/${id}`, {
      ...json(body),
      method: "PATCH",
    }),
  deleteCalendar: (id: number) =>
    request<void>(`/calendars/${id}`, { method: "DELETE" }),

  taskTemplates: () =>
    request<
      {
        name: string;
        display_name: string;
        duration_default: string;
        schedule_mode: string;
        task_api: string | null;
        api_mode: string | null;
        api_config: Record<string, unknown>;
        on_failure: string;
        allow_manual_override: boolean;
      }[]
    >("/task-templates"),
  putTaskTemplate: (name: string, body: Record<string, unknown>) =>
    request<{ name: string }>(`/task-templates/${name}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  deleteTaskTemplate: (name: string) =>
    request<void>(`/task-templates/${name}`, { method: "DELETE" }),
  handlers: () =>
    request<{ name: string; builtin: boolean; description: string }[]>(
      "/handlers",
    ),
};

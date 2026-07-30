/**
 * Typed fetch wrapper.
 *
 * The API returns one error envelope for every failure, so a single class can
 * carry the domain code through to the UI. Components branch on `code` rather
 * than parsing messages.
 */

import type {
  ApiErrorBody,
  CaseDetail,
  CaseSummary,
  HealthCounts,
  Me,
  MyTask,
  Preview,
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
    request<{ template_name: string; draft_version: number }>(
      "/templates/import",
      json({ document }),
    ),
  taskTemplates: () =>
    request<{ name: string; display_name: string; task_api: string | null }[]>(
      "/task-templates",
    ),
  handlers: () =>
    request<{ name: string; builtin: boolean; description: string }[]>(
      "/handlers",
    ),
};

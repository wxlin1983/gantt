/** Shapes returned by the API. Mirrors app/api/schemas.py. */

export type TaskStatus =
  | "pending"
  | "ready"
  | "running"
  | "done"
  | "failed"
  | "cancelled";

export type CaseStatus = "active" | "completed" | "cancelled";
export type CaseHealth = "on_track" | "at_risk" | "overdue";

export interface User {
  id: number;
  username: string;
  display_name: string;
  email: string | null;
  is_template_admin: boolean;
}

export interface Me {
  user: User;
  groups: string[];
  lead_of: string[];
}

export interface Task {
  id: number;
  name: string;
  display_name: string;
  phase: string;
  status: TaskStatus;
  owner_id: number | null;
  owner_source: string;
  group_id: number | null;
  duration_seconds: number;
  baseline_start: string | null;
  baseline_end: string | null;
  forecast_start: string | null;
  forecast_end: string | null;
  actual_start: string | null;
  actual_end: string | null;
  is_on_critical_path: boolean;
  is_optional: boolean;
  /** True for tasks added after creation; they have no baseline to compare. */
  is_unplanned: boolean;
  task_api: string | null;
  completion_source: "manual" | "api" | null;
  completion_note: string;
  version: number;
  permissions: Record<string, boolean>;
}

export interface Edge {
  predecessor: string;
  successor: string;
  lag_seconds: number;
}

export interface Skipped {
  id: string;
  label: string;
  reason: string;
}

export interface CaseSummary {
  id: number;
  name: string;
  template_name: string;
  template_version: number;
  status: CaseStatus;
  health: CaseHealth | null;
  target_date: string;
  forecast_end: string | null;
  progress_ratio: number | null;
  buffer_consumed_ratio: number | null;
  owner_id: number | null;
  /** Resolved server-side; empty when the case has no owner. */
  owner_name: string;
  created_at: string;
  blocked_on: string[];
  exceeds_target_by_seconds: number;
}

export interface CaseDetail extends CaseSummary {
  params: Record<string, unknown>;
  role_assignments: Record<string, string>;
  buffer_seconds: number;
  target_date_history: {
    from: string;
    to: string;
    by: number;
    at: string;
    note: string;
  }[];
  skipped_tasks: Skipped[];
  tasks: Task[];
  dependencies: Edge[];
  permissions: Record<string, boolean>;
  version: number;
  /** user id -> display name, for every owner this case refers to. */
  people: Record<string, string>;
  /** group id -> name. */
  groups: Record<string, string>;
}

export interface PreviewTask {
  name: string;
  display_name: string;
  phase: string;
  owner: string | null;
  group: string;
  duration_seconds: number;
  baseline_start: string;
  baseline_end: string;
  is_on_critical_path: boolean;
  is_optional: boolean;
}

export interface Preview {
  tasks: PreviewTask[];
  dependencies: Edge[];
  skipped_tasks: Skipped[];
  earliest_start: string;
  plan_deadline: string;
  target_date: string;
  buffer_seconds: number;
  critical_path_seconds: number;
  critical_path: string[];
  feasible: boolean;
  slack_seconds: number;
  warnings: { code: string; message: string; path: string }[];
}

export interface HealthCounts {
  on_track: number;
  at_risk: number;
  overdue: number;
  completed: number;
  cancelled: number;
}

export interface MyTask {
  case_id: number;
  case_name: string;
  task_id: number;
  name: string;
  display_name: string;
  status: TaskStatus;
  baseline_start: string | null;
  baseline_end: string | null;
  forecast_end: string | null;
  is_late_start: boolean;
}

export interface TemplateListItem {
  name: string;
  version: number;
  description: string;
  step_count: number;
  active_cases: number;
  has_draft: boolean;
  published_at: string | null;
}

export interface TemplateDetail {
  name: string;
  latest_version: number | null;
  definition: Record<string, unknown>;
  draft: { version: number; definition: Record<string, unknown> } | null;
  versions: {
    version: number;
    status: string;
    change_note: string;
    published_at: string | null;
  }[];
}

export interface Issue {
  code: string;
  message: string;
  path: string;
  severity?: string;
}

export interface ValidationResult {
  ok: boolean;
  errors: Issue[];
  warnings: Issue[];
}

export interface TemplateHealth {
  template_name: string;
  case_count: number;
  on_time_ratio: number | null;
  tasks: {
    task_id: string;
    label: string;
    planned_duration_seconds: number;
    sample_size: number;
    actual_median_seconds: number;
    actual_p80_seconds: number;
    overrun_ratio: number;
    on_critical_path_ratio: number;
    suggestion: string;
  }[];
  bottlenecks: { task_id: string; reason: string }[];
}

export interface ApiErrorBody {
  error: { code: string; message: string; details: Record<string, unknown> };
}

export interface TaskRun {
  attempt: number;
  handler_name: string;
  status: string;
  external_ref: string | null;
  error_message: string | null;
  error_detail: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface Simulation {
  current_forecast_end: string | null;
  simulated_forecast_end: string | null;
  delta_seconds: number;
  affected: { name: string; delta_seconds: number }[];
  exceeds_target: boolean;
  exceeds_target_by_seconds: number;
}

export interface Notification {
  id: number;
  type: string;
  title: string;
  body: string;
  case_id: number | null;
  case_task_id: number | null;
  read_at: string | null;
  created_at: string;
}

export interface AuditEvent {
  id: number;
  event_type: string;
  actor_id: number | null;
  case_task_id: number | null;
  note: string;
  created_at: string;
  before_state: Record<string, unknown> | null;
  after_state: Record<string, unknown> | null;
}

export interface GroupMembership {
  group_id: number;
  is_lead: boolean;
}

/** A directory entry. The password hash never leaves the server. */
export interface Person {
  id: number;
  username: string;
  display_name: string;
  email: string | null;
  is_active: boolean;
  is_template_admin: boolean;
  has_password: boolean;
  memberships: GroupMembership[];
}

export interface GroupMember {
  user_id: number;
  username: string;
  display_name: string;
  is_lead: boolean;
}

export interface GroupDetail {
  id: number;
  name: string;
  display_name: string;
  members: GroupMember[];
}

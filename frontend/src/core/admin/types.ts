/**
 * Admin Console API types — mirror the PR-060 backend response models in
 * `backend/app/gateway/routers/admin.py`. These are plain interfaces (no
 * zod runtime validation) because the gateway is the trusted source of
 * truth and the shapes are stable; runtime parsing would only add noise.
 */

export interface AdminStats {
  org_id: string;
  window_start: string;
  window_end: string;
  total_runs: number;
  runs_by_status: Record<string, number>;
  failure_rate: number;
  recent_runs_24h: number;
  recent_failures_24h: number;
}

export interface AdminRun {
  run_id: string;
  thread_id: string;
  user_id: string | null;
  status: string;
  model_name: string | null;
  created_at: string;
  updated_at: string;
  total_tokens: number;
  error: string | null;
}

export interface AdminRunList {
  data: AdminRun[];
  has_more: boolean;
  next_cursor: string | null;
}

export interface AdminModelUsage {
  tokens: number;
  runs: number;
}

export interface AdminTokenUsage {
  org_id: string;
  window_start: string;
  window_end: string;
  total_tokens: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_runs: number;
  by_model: Record<string, AdminModelUsage>;
  by_caller: {
    lead_agent: number;
    subagent: number;
    middleware: number;
  };
}

// ── Query param shapes (forwarded to PR-060 endpoints) ────────────────

export interface AdminStatsParams {
  since?: string;
  until?: string;
}

export interface AdminRunsParams {
  status?: string;
  model?: string;
  since?: string;
  until?: string;
  limit?: number;
  cursor?: string;
}

export interface AdminUsageParams {
  since?: string;
  until?: string;
  include_active?: boolean;
}

export const ADMIN_DEFAULT_PAGE_SIZE = 50;

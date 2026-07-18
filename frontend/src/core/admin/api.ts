/**
 * Admin Console REST client — talks to PR-060's `/api/v1/admin/*` endpoints.
 *
 * Mirrors the `core/mcp/api.ts` pattern: an `AdminRequestError` carrying the
 * HTTP status so consumers can distinguish 403 (non-admin, hide UI) from
 * 5xx (gateway down, show retry). All calls go through the CSRF-wrapped
 * `fetch` from `@/core/api/fetcher` — credentials, X-CSRF-Token injection,
 * and 401→/login redirect are free.
 */

import { fetch } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";

import type {
  AdminRunsParams,
  AdminStats,
  AdminStatsParams,
  AdminRunList,
  AdminTokenUsage,
  AdminUsageParams,
} from "./types";

export class AdminRequestError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "AdminRequestError";
    this.status = status;
  }
  /** 403 — caller is authenticated but not an admin (system_role !== "admin"). */
  get isAdminRequired(): boolean {
    return this.status === 403;
  }
}

async function readErrorDetail(
  response: Response,
  fallback: string,
): Promise<string> {
  // FastAPI errors come back as {"detail": "..."} or {"detail": {code,message}}.
  const error = (await response.json().catch(() => ({}))) as {
    detail?: unknown;
  };
  const detail = error.detail;
  if (typeof detail === "string") return detail;
  if (
    detail !== null &&
    typeof detail === "object" &&
    "message" in detail &&
    typeof (detail as { message: unknown }).message === "string"
  ) {
    return (detail as { message: string }).message;
  }
  return fallback;
}

function buildURL(path: string, params?: object): string {
  const base = `${getBackendBaseURL()}${path}`;
  if (!params) return base;
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params as Record<string, unknown>)) {
    if (value === undefined || value === null || value === "") continue;
    // Coerce scalars to strings; URLSearchParams.set would otherwise call
    // the default Object stringification for objects. We only ever forward
    // string/number/boolean query params from the admin endpoints.
    if (typeof value === "string") {
      search.set(key, value);
    } else if (typeof value === "number" || typeof value === "boolean") {
      search.set(key, value.toString());
    } else {
      // Skip objects/arrays — no admin endpoint takes them today.
      continue;
    }
  }
  const qs = search.toString();
  return qs ? `${base}?${qs}` : base;
}

export async function fetchAdminStats(
  params?: AdminStatsParams,
): Promise<AdminStats> {
  const response = await fetch(buildURL("/api/v1/admin/stats", params));
  if (!response.ok) {
    throw new AdminRequestError(
      response.status,
      await readErrorDetail(response, "Failed to load org stats"),
    );
  }
  return response.json() as Promise<AdminStats>;
}

export async function fetchAdminRuns(
  params: AdminRunsParams,
): Promise<AdminRunList> {
  const response = await fetch(buildURL("/api/v1/admin/runs", params));
  if (!response.ok) {
    throw new AdminRequestError(
      response.status,
      await readErrorDetail(response, "Failed to load runs"),
    );
  }
  return response.json() as Promise<AdminRunList>;
}

export async function fetchAdminUsage(
  params?: AdminUsageParams,
): Promise<AdminTokenUsage> {
  const response = await fetch(buildURL("/api/v1/admin/usage", params));
  if (!response.ok) {
    throw new AdminRequestError(
      response.status,
      await readErrorDetail(response, "Failed to load token usage"),
    );
  }
  return response.json() as Promise<AdminTokenUsage>;
}

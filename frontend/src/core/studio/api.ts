/**
 * Studio domain REST client — talks to the Agent artifact & release API
 * (PR-050~056, `/api/v1/agent-packages*` etc).
 *
 * Mirrors the `core/admin/api.ts` pattern: a `StudioRequestError` carrying the
 * HTTP status so consumers can distinguish 403 (no studio permission — the
 * backend RBAC gate) from 409 (CAS / idempotency / gate conflict) from 5xx
 * (gateway down, show retry). The `ContractError` envelope
 * `{detail: {code, message, retryable}}` is parsed so the UI can surface the
 * exact code and retryability to the operator (release errors are non-retryable
 * except `release_conflict`).
 *
 * All calls go through the CSRF-wrapped `fetch` from `@/core/api/fetcher` —
 * credentials, X-CSRF-Token injection, and 401→/login redirect are free.
 *
 * CAS dual-track (PR-055): promote/rollback send `If-Match: "<row_version>"`
 * (header precedence) and an optional `Idempotency-Key` header for safe replay.
 */

import { fetch } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";

import type {
  AgentPackage,
  AgentVersion,
  ChannelMoveRequest,
  ChannelMoveResponse,
  CreatePackageRequest,
  ImportAgentRequest,
  ImportReport,
  ReconcileReport,
  ReleaseChannel,
  ReleaseChannelName,
  ReleaseEvent,
  UpdatePackageRequest,
} from "./types";

export class StudioRequestError extends Error {
  readonly status: number;
  /** Backend ContractError code (e.g. "release_conflict"), or null if absent. */
  readonly code: string | null;
  /** Whether the backend marked this error retryable (ContractError.retryable). */
  readonly retryable: boolean;
  constructor(
    status: number,
    message: string,
    code: string | null = null,
    retryable = false,
  ) {
    super(message);
    this.name = "StudioRequestError";
    this.status = status;
    this.code = code;
    this.retryable = retryable;
  }
  /** 403 — caller is authenticated but lacks the studio:* permission (backend RBAC). */
  get isPermissionDenied(): boolean {
    return this.status === 403;
  }
}

/**
 * Parse a FastAPI error body into {message, code, retryable}.
 *
 * Release/artifact errors come back as `{"detail": {code, message, retryable,
 * request_id, details}}` (ContractError envelope, errors.md §12). Legacy
 * string-detail errors fall back to the raw string. A malformed body falls back
 * to the caller-provided message.
 */
async function readErrorDetail(
  response: Response,
  fallback: string,
): Promise<{ message: string; code: string | null; retryable: boolean }> {
  const error = (await response.json().catch(() => ({}))) as {
    detail?: unknown;
  };
  const detail = error.detail;
  if (typeof detail === "string") {
    return { message: detail, code: null, retryable: false };
  }
  if (
    detail !== null &&
    typeof detail === "object" &&
    "message" in detail &&
    typeof (detail as { message: unknown }).message === "string"
  ) {
    const d = detail as {
      message: string;
      code?: string;
      retryable?: boolean;
    };
    return {
      message: d.message,
      code: typeof d.code === "string" ? d.code : null,
      retryable: typeof d.retryable === "boolean" ? d.retryable : false,
    };
  }
  return { message: fallback, code: null, retryable: false };
}

/** Throw a StudioRequestError if !ok, parsing the ContractError envelope. */
async function ensureOk(response: Response, fallback: string): Promise<void> {
  if (response.ok) return;
  const { message, code, retryable } = await readErrorDetail(
    response,
    fallback,
  );
  throw new StudioRequestError(response.status, message, code, retryable);
}

// ── Helpers ───────────────────────────────────────────────────────────

function apiUrl(path: string): string {
  return `${getBackendBaseURL()}${path}`;
}

/**
 * Build headers for a state-changing request, adding the CAS `If-Match` and
 * `Idempotency-Key` headers when supplied. The CSRF `X-CSRF-Token` is injected
 * by the shared `fetch` wrapper (it reads the cookie itself), so callers do not
 * set it here.
 */
function buildMutationHeaders(options?: {
  ifMatch?: number;
  idempotencyKey?: string;
}): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (options?.ifMatch !== undefined) {
    // If-Match takes a quoted integer (PR-055 ETag shape).
    headers["If-Match"] = `"${options.ifMatch}"`;
  }
  if (options?.idempotencyKey) {
    headers["Idempotency-Key"] = options.idempotencyKey;
  }
  return headers;
}

// ── Package fetchers (GET) ────────────────────────────────────────────

export async function listPackages(): Promise<AgentPackage[]> {
  const response = await fetch(apiUrl("/api/v1/agent-packages"));
  await ensureOk(response, "Failed to load agent packages");
  return (await response.json()) as AgentPackage[];
}

export async function getPackage(packageId: string): Promise<AgentPackage> {
  const response = await fetch(apiUrl(`/api/v1/agent-packages/${packageId}`));
  await ensureOk(response, "Failed to load agent package");
  return (await response.json()) as AgentPackage;
}

// ── Package mutations ─────────────────────────────────────────────────

export async function createPackage(
  request: CreatePackageRequest,
): Promise<AgentPackage> {
  const response = await fetch(apiUrl("/api/v1/agent-packages"), {
    method: "POST",
    headers: buildMutationHeaders(),
    body: JSON.stringify(request),
  });
  await ensureOk(response, "Failed to create agent package");
  return (await response.json()) as AgentPackage;
}

export async function updatePackage(
  packageId: string,
  request: UpdatePackageRequest,
): Promise<AgentPackage> {
  const response = await fetch(apiUrl(`/api/v1/agent-packages/${packageId}`), {
    method: "PATCH",
    headers: buildMutationHeaders(),
    body: JSON.stringify(request),
  });
  await ensureOk(response, "Failed to update agent package");
  return (await response.json()) as AgentPackage;
}

export async function archivePackage(packageId: string): Promise<AgentPackage> {
  const response = await fetch(
    apiUrl(`/api/v1/agent-packages/${packageId}:archive`),
    {
      method: "POST",
      headers: buildMutationHeaders(),
    },
  );
  await ensureOk(response, "Failed to archive agent package");
  return (await response.json()) as AgentPackage;
}

// ── Version fetchers (GET) ────────────────────────────────────────────

export async function listVersions(packageId: string): Promise<AgentVersion[]> {
  const response = await fetch(
    apiUrl(`/api/v1/agent-packages/${packageId}/versions`),
  );
  await ensureOk(response, "Failed to load versions");
  return (await response.json()) as AgentVersion[];
}

// ── Version lifecycle mutations ───────────────────────────────────────

async function versionStateChange(
  versionId: string,
  action: "review" | "publish" | "revoke",
): Promise<AgentVersion> {
  const response = await fetch(
    apiUrl(`/api/v1/agent-versions/${versionId}:${action}`),
    {
      method: "POST",
      headers: buildMutationHeaders(),
    },
  );
  await ensureOk(response, `Failed to ${action} version`);
  return (await response.json()) as AgentVersion;
}

export function reviewVersion(versionId: string): Promise<AgentVersion> {
  return versionStateChange(versionId, "review");
}

export function publishVersion(versionId: string): Promise<AgentVersion> {
  return versionStateChange(versionId, "publish");
}

export function revokeVersion(versionId: string): Promise<AgentVersion> {
  return versionStateChange(versionId, "revoke");
}

// ── Channel fetchers (GET) ────────────────────────────────────────────

export async function listChannels(
  packageId: string,
): Promise<ReleaseChannel[]> {
  const response = await fetch(
    apiUrl(`/api/v1/agent-packages/${packageId}/channels`),
  );
  await ensureOk(response, "Failed to load channels");
  return (await response.json()) as ReleaseChannel[];
}

export async function getChannel(
  packageId: string,
  channel: ReleaseChannelName,
): Promise<ReleaseChannel> {
  const response = await fetch(
    apiUrl(`/api/v1/agent-packages/${packageId}/channels/${channel}`),
  );
  await ensureOk(response, "Failed to load channel");
  return (await response.json()) as ReleaseChannel;
}

export async function listChannelEvents(
  packageId: string,
  channel: ReleaseChannelName,
): Promise<ReleaseEvent[]> {
  const response = await fetch(
    apiUrl(`/api/v1/agent-packages/${packageId}/channels/${channel}/events`),
  );
  await ensureOk(response, "Failed to load channel events");
  return (await response.json()) as ReleaseEvent[];
}

// ── Channel move mutations (CAS + Idempotency-Key, PR-053/055) ────────

/**
 * Promote a version onto a channel. Sends `If-Match` (CAS) from the channel's
 * current row_version and an `Idempotency-Key` for safe replay. The hook fills
 * these from the loaded channel + a fresh UUID; callers may override.
 */
export async function promoteChannel(
  packageId: string,
  channel: ReleaseChannelName,
  request: ChannelMoveRequest,
  options?: { ifMatch?: number; idempotencyKey?: string },
): Promise<ChannelMoveResponse> {
  return channelMove(packageId, channel, "promote", request, options);
}

export async function rollbackChannel(
  packageId: string,
  channel: ReleaseChannelName,
  request: ChannelMoveRequest,
  options?: { ifMatch?: number; idempotencyKey?: string },
): Promise<ChannelMoveResponse> {
  return channelMove(packageId, channel, "rollback", request, options);
}

async function channelMove(
  packageId: string,
  channel: ReleaseChannelName,
  action: "promote" | "rollback",
  request: ChannelMoveRequest,
  options?: { ifMatch?: number; idempotencyKey?: string },
): Promise<ChannelMoveResponse> {
  // The body omits expected_channel_version when If-Match is present (header
  // precedence; exactly-one validation, PR-055). When no If-Match is supplied,
  // fall back to the body field.
  const ifMatch = options?.ifMatch ?? request.expected_channel_version;
  const body: Record<string, unknown> = {
    target_version_id: request.target_version_id,
    reason: request.reason,
    workspace_id: request.workspace_id,
  };
  if (ifMatch === undefined && request.expected_channel_version !== undefined) {
    body.expected_channel_version = request.expected_channel_version;
  }
  const headers = buildMutationHeaders({
    ifMatch,
    idempotencyKey: options?.idempotencyKey ?? request.idempotency_key,
  });
  const response = await fetch(
    apiUrl(`/api/v1/agent-packages/${packageId}/channels/${channel}:${action}`),
    { method: "POST", headers, body: JSON.stringify(body) },
  );
  await ensureOk(response, `Failed to ${action} ${channel} channel`);
  return (await response.json()) as ChannelMoveResponse;
}

// ── File-state import (PR-051) ────────────────────────────────────────

export async function importAgent(
  request: ImportAgentRequest,
): Promise<ImportReport> {
  const response = await fetch(apiUrl("/api/v1/agent-packages:import-file"), {
    method: "POST",
    headers: buildMutationHeaders(),
    body: JSON.stringify(request),
  });
  await ensureOk(response, "Failed to import agent");
  return (await response.json()) as ImportReport;
}

// ── Inventory reconciliation (PR-052) ─────────────────────────────────

export async function reconcileInventory(): Promise<ReconcileReport> {
  const response = await fetch(apiUrl("/api/v1/agent-packages:reconcile"), {
    method: "POST",
    headers: buildMutationHeaders(),
  });
  await ensureOk(response, "Failed to reconcile inventory");
  return (await response.json()) as ReconcileReport;
}

/**
 * Studio domain types — plain TS interfaces mirroring the backend response
 * envelopes for the Agent artifact & release API (PR-050~056).
 *
 * Deliberately NOT zod-validated: the gateway is the trusted source (mirrors
 * `core/admin/types.ts`). Field names match the Pydantic models 1:1 so a
 * backend rename surfaces as a type error here.
 *
 * Source of truth: backend/packages/harness/deerflow/contracts/agent_artifact.py
 */

// ── AgentPackage (ADR-0004 §3.1) ──────────────────────────────────────

/** 1:1 projection of AgentPackageResponse. */
export interface AgentPackage {
  id: string;
  org_id: string;
  workspace_id: string | null;
  name: string;
  display_name: string;
  description: string | null;
  status: string; // active | archived
  created_by: string | null;
  created_at: string; // ISO 8601
  updated_at: string;
  row_version: number;
}

/** Body of POST /api/v1/agent-packages (AgentPackageCreateRequest). */
export interface CreatePackageRequest {
  name: string;
  display_name: string;
  description?: string;
  workspace_id?: string;
}

/** Body of PATCH /api/v1/agent-packages/{id} (AgentPackageUpdateRequest). */
export interface UpdatePackageRequest {
  display_name?: string;
  description?: string;
}

// ── AgentVersion (ADR-0004 §3.2) ──────────────────────────────────────

/** 1:1 projection of AgentVersionResponse (content_inline deliberately omitted). */
export interface AgentVersion {
  id: string;
  org_id: string;
  package_id: string;
  version: string; // SemVer display string
  digest: string; // sha256:<hex> — immutable execution identity
  status: VersionStatus;
  manifest: Record<string, unknown>;
  object_key: string | null;
  size_bytes: number;
  created_by: string | null;
  created_at: string;
  published_at: string | null;
  revoked_at: string | null;
}

/** Closed status set for an AgentVersion (ADR §4 state machine). */
export type VersionStatus =
  | "draft"
  | "reviewed"
  | "published"
  | "revoked"
  | "archived";

// ── Manifest (ADR-0004 §3.3) ─────────────────────────────────────────
//
// The backend `Manifest` model declares the list fields as bare `list[dict]`
// with NO sub-key validation (extra="forbid" applies to the top-level field
// set only). The sub-key shapes below are a product decision based on:
//   - importer.py (the only producer of non-empty model_requirements/skills)
//   - test_release_schema.py fixtures (skills id/version, secret name/ref)
//   - ADR §3.3 intent ("record stable ID, version or digest; deps/network
//     must be explicit; Secret is a ref, never plaintext")
// The backend persists whatever keys are sent, so these interfaces are the
// authoritative shape the Studio editor produces and round-trips.

export interface ModelRequirement {
  name: string;
}

export interface SkillRef {
  name: string;
  version?: string;
  digest?: string;
}

export interface McpServerRef {
  name: string;
  version?: string;
}

export interface DependencyLock {
  name: string;
  version?: string;
  source?: string;
}

export interface NetworkRequirement {
  host: string;
  port?: number;
  protocol?: string;
}

export interface SecretRequirement {
  name: string;
  ref: string;
}

export interface RuntimeLimits {
  max_steps?: number;
  max_tokens?: number;
  timeout_s?: number;
}

export interface AgentManifest {
  schema_version: string;
  agent_entry: string;
  soul_or_prompt_ref?: string | null;
  model_requirements?: ModelRequirement[];
  skills?: SkillRef[];
  tools?: string[];
  mcp_servers?: McpServerRef[];
  dependencies?: DependencyLock[];
  network_requirements?: NetworkRequirement[];
  secret_requirements?: SecretRequirement[];
  runtime_limits?: RuntimeLimits | null;
  source_metadata?: Record<string, unknown> | null;
}

/** Body of POST /api/v1/agent-packages/{pkg}/versions. */
export interface CreateVersionRequest {
  version: string; // SemVer 2.0 display string
  manifest: AgentManifest;
  content: string; // raw artifact payload (UTF-8); digest computed server-side
}

// ── Release channels & events (ADR-0004 §5/§7/§8) ────────────────────

/** Closed channel set. */
export type ReleaseChannelName = "dev" | "staging" | "prod";

/** 1:1 projection of ReleaseChannelResponse. */
export interface ReleaseChannel {
  id: string;
  org_id: string;
  workspace_id: string | null;
  package_id: string;
  channel: ReleaseChannelName;
  current_version_id: string | null; // NULL = channel exists but empty
  row_version: number; // CAS token — echo as If-Match / expected_channel_version
  updated_by: string | null;
  created_at: string;
  updated_at: string;
}

/** 1:1 projection of ReleaseEventResponse (domain history, §14). */
export interface ReleaseEvent {
  id: string;
  org_id: string;
  channel_id: string;
  from_version_id: string | null; // NULL on first promote
  to_version_id: string;
  action: "promote" | "rollback";
  actor_type: string | null;
  actor_id: string | null;
  reason: string | null;
  created_at: string;
}

/** Body of POST .../channels/{ch}:promote / :rollback (PromoteRequest / RollbackRequest). */
export interface ChannelMoveRequest {
  target_version_id: string;
  /** CAS predicate — the channel row's current row_version. */
  expected_channel_version?: number;
  /** Optional UUID for safe replay (PR-055). Generated by the UI if absent. */
  idempotency_key?: string;
  workspace_id?: string;
  reason?: string;
}

/** 1:1 projection of PromoteResponse (promote / rollback result). */
export interface ChannelMoveResponse {
  channel: ReleaseChannel;
  event: ReleaseEvent;
}

// ── File-state import (ADR-0004 §10, PR-051) ──────────────────────────

/** Body of POST /api/v1/agent-packages:import-file (ImportFileRequest). */
export interface ImportAgentRequest {
  name: string;
  version: string; // SemVer 2.0
  user_id?: string | null;
  display_name?: string;
  description?: string;
  workspace_id?: string;
}

/** 1:1 projection of ImportReport. */
export interface ImportReport {
  package: AgentPackage;
  version: AgentVersion;
  digest: string;
  imported: boolean; // false = idempotent re-import returned existing version
  source_metadata: Record<string, unknown>;
}

// ── Inventory reconciliation (PR-052) ─────────────────────────────────

/** Result of POST /api/v1/agent-packages:reconcile (reconcile_inventory dict). */
export interface ReconcileReport {
  org_id: string;
  checked_count: number;
  is_clean: boolean;
  missing_versions: Array<{
    version_id: string;
    object_key: string;
    reason: string;
  }>;
}

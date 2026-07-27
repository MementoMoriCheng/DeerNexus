/**
 * TanStack Query hooks for the Studio domain (Agent artifact & release API).
 *
 * Reads use `useQuery`. Writes (review/publish/revoke/promote/rollback/import/
 * create/archive) use `useMutation` with:
 *   - `onSuccess`: a sonner `toast.success` + `queryClient.invalidateQueries`
 *     on the whole `["studio", ...]` namespace (lists + detail strong-consistent
 *     after a write; channels carry a fresh row_version for the next CAS).
 *   - `onError`: a sonner `toast.error` surfacing the backend ContractError
 *     `{code, message, retryable}` (release errors are non-retryable except
 *     `release_conflict`; the UI may re-enable a retry button on retryable).
 *
 * The promote/rollback mutations carry the CAS `If-Match` (from the loaded
 * channel's `row_version`) and a generated `Idempotency-Key` so a duplicate
 * click replays the original result instead of double-applying.
 *
 * All hooks inherit the default options from the shared `QueryClient`
 * (`refetchOnWindowFocus: false`).
 */

"use client";

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryOptions,
} from "@tanstack/react-query";
import { toast } from "sonner";

import {
  archivePackage,
  createPackage,
  getPackage,
  importAgent,
  listChannelEvents,
  listChannels,
  listPackages,
  listVersions,
  promoteChannel,
  publishVersion,
  reconcileInventory,
  reviewVersion,
  revokeVersion,
  rollbackChannel,
  StudioRequestError,
  updatePackage,
} from "./api";
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

/** Query-key root for the whole Studio namespace (invalidated after any write). */
const STUDIO_KEY = ["studio"] as const;

/** Common query options so callers cannot clobber the key/queryFn. */
type ReadOptions<T> = Omit<UseQueryOptions<T>, "queryKey" | "queryFn">;

// ── Reads ─────────────────────────────────────────────────────────────

export function useStudioPackages(options?: ReadOptions<AgentPackage[]>) {
  return useQuery({
    queryKey: [...STUDIO_KEY, "packages"],
    queryFn: listPackages,
    ...options,
  });
}

export function useStudioPackage(
  packageId: string,
  options?: ReadOptions<AgentPackage>,
) {
  return useQuery({
    queryKey: [...STUDIO_KEY, "packages", packageId],
    queryFn: () => getPackage(packageId),
    enabled: Boolean(packageId),
    ...options,
  });
}

export function useStudioVersions(
  packageId: string,
  options?: ReadOptions<AgentVersion[]>,
) {
  return useQuery({
    queryKey: [...STUDIO_KEY, "packages", packageId, "versions"],
    queryFn: () => listVersions(packageId),
    enabled: Boolean(packageId),
    ...options,
  });
}

export function useStudioChannels(
  packageId: string,
  options?: ReadOptions<ReleaseChannel[]>,
) {
  return useQuery({
    queryKey: [...STUDIO_KEY, "packages", packageId, "channels"],
    queryFn: () => listChannels(packageId),
    enabled: Boolean(packageId),
    ...options,
  });
}

export function useStudioChannelEvents(
  packageId: string,
  channel: ReleaseChannelName | null,
  options?: ReadOptions<ReleaseEvent[]>,
) {
  return useQuery({
    queryKey: [
      ...STUDIO_KEY,
      "packages",
      packageId,
      "channels",
      channel,
      "events",
    ],
    queryFn: () => listChannelEvents(packageId, channel!),
    enabled: Boolean(packageId) && channel !== null,
    ...options,
  });
}

// ── Mutation helpers ──────────────────────────────────────────────────

/** Format a StudioRequestError for a toast. */
function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof StudioRequestError) {
    const codeTag = error.code ? ` (${error.code})` : "";
    return `${error.message}${codeTag}`;
  }
  if (error instanceof Error) return error.message || fallback;
  return fallback;
}

/** Invalidate the whole Studio namespace after a successful write. */
function useInvalidateStudio() {
  const queryClient = useQueryClient();
  return () => queryClient.invalidateQueries({ queryKey: STUDIO_KEY });
}

// ── Version lifecycle mutations ───────────────────────────────────────

export function useReviewVersion(): UseMutationResult<
  AgentVersion,
  Error,
  string
> {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: reviewVersion,
    onSuccess: (version) => {
      toast.success(`Version ${version.version} submitted for review`);
      void invalidate();
    },
    onError: (error) =>
      toast.error(errorMessage(error, "Failed to review version")),
  });
}

export function usePublishVersion(): UseMutationResult<
  AgentVersion,
  Error,
  string
> {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: publishVersion,
    onSuccess: (version) => {
      toast.success(`Version ${version.version} published`);
      void invalidate();
    },
    onError: (error) =>
      toast.error(errorMessage(error, "Failed to publish version")),
  });
}

export function useRevokeVersion(): UseMutationResult<
  AgentVersion,
  Error,
  string
> {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: revokeVersion,
    onSuccess: (version) => {
      toast.success(`Version ${version.version} revoked`);
      void invalidate();
    },
    onError: (error) =>
      toast.error(errorMessage(error, "Failed to revoke version")),
  });
}

// ── Channel move mutations (CAS + Idempotency-Key) ────────────────────

/** Args for promote/rollback: identifies the target + the CAS context. */
interface ChannelMoveArgs {
  packageId: string;
  channel: ReleaseChannelName;
  request: ChannelMoveRequest;
}

export function usePromoteChannel(): UseMutationResult<
  ChannelMoveResponse,
  Error,
  ChannelMoveArgs
> {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: ({ packageId, channel, request }) =>
      promoteChannel(packageId, channel, request, {
        ifMatch: request.expected_channel_version,
        idempotencyKey: request.idempotency_key ?? crypto.randomUUID(),
      }),
    onSuccess: (_data, variables) => {
      toast.success(`Promoted to ${variables.channel}`);
      void invalidate();
    },
    onError: (error) => toast.error(errorMessage(error, "Failed to promote")),
  });
}

export function useRollbackChannel(): UseMutationResult<
  ChannelMoveResponse,
  Error,
  ChannelMoveArgs
> {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: ({ packageId, channel, request }) =>
      rollbackChannel(packageId, channel, request, {
        ifMatch: request.expected_channel_version,
        idempotencyKey: request.idempotency_key ?? crypto.randomUUID(),
      }),
    onSuccess: (_data, variables) => {
      toast.success(`Rolled back ${variables.channel}`);
      void invalidate();
    },
    onError: (error) => toast.error(errorMessage(error, "Failed to rollback")),
  });
}

// ── Import mutation ───────────────────────────────────────────────────

export function useImportAgent(): UseMutationResult<
  ImportReport,
  Error,
  ImportAgentRequest
> {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: importAgent,
    onSuccess: (report) => {
      toast.success(
        report.imported
          ? `Imported ${report.package.name} v${report.version.version}`
          : `Re-import (idempotent): ${report.package.name} v${report.version.version} already exists`,
      );
      void invalidate();
    },
    onError: (error) =>
      toast.error(errorMessage(error, "Failed to import agent")),
  });
}

// ── Package CRUD mutations ────────────────────────────────────────────

export function useCreatePackage(): UseMutationResult<
  AgentPackage,
  Error,
  CreatePackageRequest
> {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: createPackage,
    onSuccess: (pkg) => {
      toast.success(`Package ${pkg.name} created`);
      void invalidate();
    },
    onError: (error) =>
      toast.error(errorMessage(error, "Failed to create package")),
  });
}

export function useUpdatePackage(): UseMutationResult<
  AgentPackage,
  Error,
  { packageId: string; request: UpdatePackageRequest }
> {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: ({ packageId, request }) => updatePackage(packageId, request),
    onSuccess: (pkg) => {
      toast.success(`Package ${pkg.name} updated`);
      void invalidate();
    },
    onError: (error) =>
      toast.error(errorMessage(error, "Failed to update package")),
  });
}

export function useArchivePackage(): UseMutationResult<
  AgentPackage,
  Error,
  string
> {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: archivePackage,
    onSuccess: (pkg) => {
      toast.success(`Package ${pkg.name} archived`);
      void invalidate();
    },
    onError: (error) =>
      toast.error(errorMessage(error, "Failed to archive package")),
  });
}

// ── Inventory reconciliation ──────────────────────────────────────────

export function useReconcileInventory(): UseMutationResult<
  ReconcileReport,
  Error,
  void
> {
  const invalidate = useInvalidateStudio();
  return useMutation({
    mutationFn: reconcileInventory,
    onSuccess: (report) => {
      if (report.is_clean) {
        toast.success(`Inventory clean (${report.checked_count} checked)`);
      } else {
        toast.warning(
          `Inventory drift: ${report.missing_versions.length} missing version(s)`,
        );
      }
      void invalidate();
    },
    onError: (error) =>
      toast.error(errorMessage(error, "Failed to reconcile inventory")),
  });
}

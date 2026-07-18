/**
 * TanStack Query hooks for the Admin Console API.
 *
 * Reads use `useQuery` (single fetch). The runs list uses `useInfiniteQuery`
 * keyed on PR-060's `next_cursor` (keyset pagination — no total page count);
 * the "Load more" button calls `fetchNextPage()`.
 *
 * All hooks inherit the default options from the shared `QueryClient`:
 * `refetchOnWindowFocus: false`. Errors surface as `isError` / `error` on
 * each hook; callers render an appropriate Empty/error state.
 */

"use client";

import {
  useInfiniteQuery,
  useQuery,
  type UseQueryOptions,
} from "@tanstack/react-query";

import {
  fetchAdminRuns,
  fetchAdminStats,
  fetchAdminUsage,
} from "./api";
import {
  ADMIN_DEFAULT_PAGE_SIZE,
  type AdminRunList,
  type AdminRunsParams,
  type AdminStats,
  type AdminStatsParams,
  type AdminTokenUsage,
  type AdminUsageParams,
} from "./types";

// ── Stats ─────────────────────────────────────────────────────────────

export function useAdminStats(
  params: AdminStatsParams = {},
  options?: Omit<UseQueryOptions<AdminStats>, "queryKey" | "queryFn">,
) {
  return useQuery({
    queryKey: ["admin", "stats", params],
    queryFn: () => fetchAdminStats(params),
    ...options,
  });
}

// ── Runs (infinite / keyset) ──────────────────────────────────────────

export type AdminRunsInfiniteParams = Omit<AdminRunsParams, "cursor">;

export function useAdminRuns(params: AdminRunsInfiniteParams) {
  return useInfiniteQuery({
    queryKey: ["admin", "runs", params],
    queryFn: ({ pageParam }: { pageParam: string | undefined }) =>
      fetchAdminRuns({
        ...params,
        limit: params.limit ?? ADMIN_DEFAULT_PAGE_SIZE,
        cursor: pageParam ?? undefined,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage: AdminRunList) =>
      lastPage.next_cursor ?? undefined,
  });
}

// ── Usage ─────────────────────────────────────────────────────────────

export function useAdminUsage(
  params: AdminUsageParams = {},
  options?: Omit<UseQueryOptions<AdminTokenUsage>, "queryKey" | "queryFn">,
) {
  return useQuery({
    queryKey: ["admin", "usage", params],
    queryFn: () => fetchAdminUsage(params),
    ...options,
  });
}

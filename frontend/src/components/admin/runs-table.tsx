"use client";

import { AlertCircleIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useAdminRuns, type AdminRunsInfiniteParams } from "@/core/admin";
import { formatTokenCount } from "@/core/messages/usage";
import { formatTimeAgo } from "@/core/utils/datetime";

import { RunStatusBadge, TruncatedCell } from "./run-status-badge";

/**
 * Keyset-paginated runs table for the Org Console.
 *
 * Consumes PR-060's `/api/v1/admin/runs` via `useAdminRuns` (infinite query
 * keyed on `next_cursor`). The "Load more" button calls `fetchNextPage`
 * until `!hasMorePage`; the page size is determined server-side (default 50).
 *
 * Reused by both `/admin/runs` (no status pre-filter) and `/admin/audit`
 * (pre-filtered to `error|timeout|interrupted`).
 */
export function RunsTable({
  params,
  emptyTitle = "No runs in this window",
  emptyDescription = "Adjust the filters above or widen the time window.",
}: {
  params: AdminRunsInfiniteParams;
  emptyTitle?: string;
  emptyDescription?: string;
}) {
  const {
    data,
    isLoading,
    isError,
    error,
    isFetchingNextPage,
    hasNextPage,
    fetchNextPage,
  } = useAdminRuns(params);

  if (isLoading) {
    return <RunsTableSkeleton />;
  }

  if (isError) {
    return (
      <Empty>
        <EmptyHeader>
          <EmptyMedia>
            <AlertCircleIcon className="text-destructive size-8" />
          </EmptyMedia>
          <EmptyTitle>Failed to load runs</EmptyTitle>
          <EmptyDescription>
            {error instanceof Error ? error.message : "Unknown error"}
          </EmptyDescription>
        </EmptyHeader>
      </Empty>
    );
  }

  const runs = data?.pages.flatMap((page) => page.data) ?? [];

  if (runs.length === 0) {
    return (
      <Empty>
        <EmptyHeader>
          <EmptyMedia>
            <AlertCircleIcon className="text-muted-foreground size-8" />
          </EmptyMedia>
          <EmptyTitle>{emptyTitle}</EmptyTitle>
          <EmptyDescription>{emptyDescription}</EmptyDescription>
        </EmptyHeader>
      </Empty>
    );
  }

  return (
    <div className="space-y-4">
      <div className="rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Run ID</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Model</TableHead>
              <TableHead className="text-right">Tokens</TableHead>
              <TableHead>User</TableHead>
              <TableHead>Created</TableHead>
              <TableHead>Error</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {runs.map((run) => (
              <TableRow key={run.run_id}>
                <TableCell>
                  <TruncatedCell value={run.run_id} maxLength={20} />
                </TableCell>
                <TableCell>
                  <RunStatusBadge status={run.status} />
                </TableCell>
                <TableCell>
                  <TruncatedCell value={run.model_name} maxLength={24} />
                </TableCell>
                <TableCell className="text-right font-mono text-xs">
                  {formatTokenCount(run.total_tokens)}
                </TableCell>
                <TableCell>
                  <TruncatedCell value={run.user_id} maxLength={16} />
                </TableCell>
                <TableCell className="text-muted-foreground text-xs">
                  {formatTimeAgo(run.created_at)}
                </TableCell>
                <TableCell className="max-w-[240px]">
                  {run.error ? (
                    <span
                      className="text-destructive line-clamp-2 text-xs"
                      title={run.error}
                    >
                      {run.error.slice(0, 200)}
                    </span>
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      {hasNextPage && (
        <div className="flex justify-center">
          <Button
            variant="outline"
            onClick={() => fetchNextPage()}
            disabled={isFetchingNextPage}
          >
            {isFetchingNextPage ? "Loading…" : "Load more"}
          </Button>
        </div>
      )}
    </div>
  );
}

function RunsTableSkeleton() {
  return (
    <div className="rounded-lg border">
      <Table>
        <TableHeader>
          <TableRow>
            {Array.from({ length: 7 }).map((_, i) => (
              <TableHead key={i}>
                <Skeleton className="h-4 w-16" />
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {Array.from({ length: 8 }).map((_, rowIdx) => (
            <TableRow key={rowIdx}>
              {Array.from({ length: 7 }).map((_, colIdx) => (
                <TableCell key={colIdx}>
                  <Skeleton className="h-4 w-full max-w-[160px]" />
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

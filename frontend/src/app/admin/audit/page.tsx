"use client";

import { AlertCircleIcon } from "lucide-react";
import { useState } from "react";

import {
  RunsFilterBar,
  windowToSince,
  type RunsFilter,
} from "@/components/admin/runs-filter-bar";
import { RunsTable } from "@/components/admin/runs-table";
import {
  Card,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { useAdminStats } from "@/core/admin";

// The audit page's status filter is pinned to the failure set.
// PR-060's `/runs` endpoint accepts a single status at a time, so the
// user picks one failure status from this dropdown (default: error).
const FAILURE_STATUSES = ["error", "timeout", "interrupted"] as const;

export default function AdminAuditPage() {
  const [filter, setFilter] = useState<RunsFilter>({
    status: "error",
    window: "7d",
  });
  const since = windowToSince(filter.window);
  const { data: stats, isLoading } = useAdminStats({ since });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Failure / Audit</h1>
        <p className="text-muted-foreground mt-1 text-sm">
          Failures are derived from run status (<code>error</code>,{" "}
          <code>timeout</code>, <code>interrupted</code>). Structured audit
          events require PR-041 (Audit outbox) — until then this view is the
          operational failure surface.
        </p>
      </div>

      {isLoading ? (
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <Card key={i}>
              <CardHeader>
                <Skeleton className="h-3 w-20" />
                <Skeleton className="h-8 w-28" />
              </CardHeader>
            </Card>
          ))}
        </div>
      ) : stats ? (
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-3">
          <Card>
            <CardHeader>
              <CardDescription>Failures (last 24h)</CardDescription>
              <CardTitle className="text-destructive text-2xl tabular-nums">
                {stats.recent_failures_24h.toLocaleString()}
              </CardTitle>
            </CardHeader>
          </Card>
          <Card>
            <CardHeader>
              <CardDescription>Failure rate (window)</CardDescription>
              <CardTitle className="text-2xl tabular-nums">
                {(stats.failure_rate * 100).toFixed(1)}%
              </CardTitle>
            </CardHeader>
          </Card>
          <Card>
            <CardHeader>
              <CardDescription>Total runs (last 24h)</CardDescription>
              <CardTitle className="text-2xl tabular-nums">
                {stats.recent_runs_24h.toLocaleString()}
              </CardTitle>
            </CardHeader>
          </Card>
        </div>
      ) : (
        <div className="text-muted-foreground flex items-center gap-2 text-sm">
          <AlertCircleIcon className="size-4" />
          Stats unavailable.
        </div>
      )}

      <div className="flex flex-wrap items-center gap-3">
        {/* Failure-status dropdown (pinned to error/timeout/interrupted). */}
        <Select
          value={filter.status ?? "error"}
          onValueChange={(value) =>
            setFilter((prev) => ({ ...prev, status: value }))
          }
        >
          <SelectTrigger className="w-[140px]" size="sm">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {FAILURE_STATUSES.map((status) => (
              <SelectItem key={status} value={status}>
                {status}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <RunsFilterBar filter={filter} onChange={setFilter} hideStatus />
      </div>

      <RunsTable
        params={{
          status: filter.status,
          since,
        }}
        emptyTitle={`No ${filter.status ?? "failure"} runs in this window`}
        emptyDescription="Adjust the time window above or pick another failure status."
      />
    </div>
  );
}

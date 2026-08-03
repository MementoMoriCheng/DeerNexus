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
import { useI18n } from "@/core/i18n/hooks";

// The audit page's status filter is pinned to the failure set.
// PR-060's `/runs` endpoint accepts a single status at a time, so the
// user picks one failure status from this dropdown (default: error).
const FAILURE_STATUSES = ["error", "timeout", "interrupted"] as const;

export default function AdminAuditPage() {
  const { t } = useI18n();
  const [filter, setFilter] = useState<RunsFilter>({
    status: "error",
    window: "7d",
  });
  const since = windowToSince(filter.window);
  const { data: stats, isLoading } = useAdminStats({ since });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">{t.admin.audit.title}</h1>
        <p className="text-muted-foreground mt-1 text-sm">
          {t.admin.audit.description}
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
              <CardDescription>{t.admin.audit.failures24h}</CardDescription>
              <CardTitle className="text-destructive text-2xl tabular-nums">
                {stats.recent_failures_24h.toLocaleString()}
              </CardTitle>
            </CardHeader>
          </Card>
          <Card>
            <CardHeader>
              <CardDescription>{t.admin.audit.failureRate}</CardDescription>
              <CardTitle className="text-2xl tabular-nums">
                {(stats.failure_rate * 100).toFixed(1)}%
              </CardTitle>
            </CardHeader>
          </Card>
          <Card>
            <CardHeader>
              <CardDescription>{t.admin.audit.totalRuns24h}</CardDescription>
              <CardTitle className="text-2xl tabular-nums">
                {stats.recent_runs_24h.toLocaleString()}
              </CardTitle>
            </CardHeader>
          </Card>
        </div>
      ) : (
        <div className="text-muted-foreground flex items-center gap-2 text-sm">
          <AlertCircleIcon className="size-4" />
          {t.admin.audit.statsUnavailable}
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
        emptyTitle={t.admin.audit.emptyTitle(filter.status ?? "failure")}
        emptyDescription={t.admin.audit.emptyDescription}
      />
    </div>
  );
}

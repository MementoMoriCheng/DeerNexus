"use client";

import { subDays } from "date-fns";
import { useState, type ReactNode } from "react";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";

/**
 * Admin time-window preset. "all" → no `since` filter (server returns the
 * full window, default 7 days for stats / unbounded for runs).
 */
export type TimeWindow = "24h" | "7d" | "30d" | "all";

export const TIME_WINDOWS: { value: TimeWindow; label: string }[] = [
  { value: "24h", label: "24h" },
  { value: "7d", label: "7d" },
  { value: "30d", label: "30d" },
  { value: "all", label: "All" },
];

export function windowToSince(window: TimeWindow): string | undefined {
  if (window === "all") return undefined;
  const days = window === "24h" ? 1 : window === "7d" ? 7 : 30;
  return subDays(new Date(), days).toISOString();
}

export const RUN_STATUSES = [
  "pending",
  "running",
  "success",
  "error",
  "timeout",
  "interrupted",
] as const;

/**
 * Filter bar shared by /admin/runs and /admin/audit.
 *
 * Plain controlled state (no form library — mirrors the settings-page
 * pattern). Filter changes propagate to the parent via `onChange`, which
 * the page uses to rebuild the query params and reset the infinite query
 * (TanStack auto-resets via the new `queryKey`).
 */
export interface RunsFilter {
  status: string | undefined;
  window: TimeWindow;
}

export function RunsFilterBar({
  filter,
  onChange,
  hideStatus = false,
  extraControls,
}: {
  filter: RunsFilter;
  onChange: (filter: RunsFilter) => void;
  /** Hide the status dropdown (audit page pre-filters status). */
  hideStatus?: boolean;
  extraControls?: ReactNode;
}) {
  const [, force] = useState(0);
  return (
    <div className="flex flex-wrap items-center gap-3">
      {!hideStatus && (
        <Select
          value={filter.status ?? "all"}
          onValueChange={(value) =>
            onChange({
              ...filter,
              status: value === "all" ? undefined : value,
            })
          }
        >
          <SelectTrigger className="w-[140px]" size="sm">
            <SelectValue placeholder="Status" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All statuses</SelectItem>
            {RUN_STATUSES.map((status) => (
              <SelectItem key={status} value={status}>
                {status}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
      <Tabs
        value={filter.window}
        onValueChange={(value) => {
          onChange({ ...filter, window: value as TimeWindow });
          force((n) => n + 1); // ensure re-render
        }}
      >
        <TabsList>
          {TIME_WINDOWS.map((w) => (
            <TabsTrigger key={w.value} value={w.value}>
              {w.label}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>
      {extraControls}
    </div>
  );
}

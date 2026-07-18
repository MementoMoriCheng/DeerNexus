"use client";

import { useState } from "react";

import {
  RunsFilterBar,
  windowToSince,
  type RunsFilter,
} from "@/components/admin/runs-filter-bar";
import { RunsTable } from "@/components/admin/runs-table";

export default function AdminRunsPage() {
  const [filter, setFilter] = useState<RunsFilter>({
    status: undefined,
    window: "7d",
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Runs</h1>
        <p className="text-muted-foreground mt-1 text-sm">
          All runs in your active Org. Use the filters to narrow by status or
          time window.
        </p>
      </div>
      <RunsFilterBar filter={filter} onChange={setFilter} />
      <RunsTable
        params={{
          status: filter.status,
          since: windowToSince(filter.window),
        }}
      />
    </div>
  );
}

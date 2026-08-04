"use client";

import { useState } from "react";

import {
  RunsFilterBar,
  windowToSince,
  type RunsFilter,
} from "@/components/admin/runs-filter-bar";
import { RunsTable } from "@/components/admin/runs-table";
import { useI18n } from "@/core/i18n/hooks";

export default function AdminRunsPage() {
  const { t } = useI18n();
  const [filter, setFilter] = useState<RunsFilter>({
    status: undefined,
    window: "7d",
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">{t.admin.runs.title}</h1>
        <p className="text-muted-foreground mt-1 text-sm">
          {t.admin.runs.description}
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

"use client";

import { useState } from "react";

import {
  TIME_WINDOWS,
  windowToSince,
  type TimeWindow,
} from "@/components/admin/runs-filter-bar";
import { UsageCharts } from "@/components/admin/usage-charts";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useI18n } from "@/core/i18n/hooks";

export default function AdminUsagePage() {
  const { t } = useI18n();
  const [window, setWindow] = useState<TimeWindow>("7d");

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">{t.admin.usage.title}</h1>
          <p className="text-muted-foreground mt-1 text-sm">
            {t.admin.usage.description}
          </p>
        </div>
        <Tabs value={window} onValueChange={(v) => setWindow(v as TimeWindow)}>
          <TabsList>
            {TIME_WINDOWS.map((w) => (
              <TabsTrigger key={w.value} value={w.value}>
                {w.value === "all" ? t.admin.filter.all : w.label}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
      </div>
      <UsageCharts since={windowToSince(window)} />
    </div>
  );
}

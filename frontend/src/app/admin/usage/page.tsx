"use client";

import { useState } from "react";

import {
  TIME_WINDOWS,
  windowToSince,
  type TimeWindow,
} from "@/components/admin/runs-filter-bar";
import { UsageCharts } from "@/components/admin/usage-charts";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";

export default function AdminUsagePage() {
  const [window, setWindow] = useState<TimeWindow>("7d");

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Usage</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Token consumption aggregated across all runs in your active Org.
          </p>
        </div>
        <Tabs value={window} onValueChange={(v) => setWindow(v as TimeWindow)}>
          <TabsList>
            {TIME_WINDOWS.map((w) => (
              <TabsTrigger key={w.value} value={w.value}>
                {w.label}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
      </div>
      <UsageCharts since={windowToSince(window)} />
    </div>
  );
}

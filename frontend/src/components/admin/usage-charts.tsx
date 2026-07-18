"use client";

import { AlertCircleIcon } from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty";
import { Skeleton } from "@/components/ui/skeleton";
import { useAdminUsage } from "@/core/admin";
import { formatTokenCount } from "@/core/messages/usage";

// Tailwind design tokens (oklch) exposed in globals.css — recharts needs the
// resolved value at draw time, so we read from getComputedStyle on mount.
const CHART_COLORS = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
];

interface ModelRow {
  model: string;
  tokens: number;
  runs: number;
}

/**
 * Token-usage view: KPI cards + by-model bar chart + by-caller breakdown.
 *
 * The `by_model` dict is converted to a top-5 + "other" bucket so the chart
 * stays readable; the table view inside the chart tooltip shows per-model
 * tokens + run count.
 */
export function UsageCharts({ since }: { since?: string }) {
  const { data, isLoading, isError, error } = useAdminUsage({
    since: since,
  });

  if (isLoading) return <UsageSkeleton />;
  if (isError) {
    return (
      <Empty>
        <EmptyHeader>
          <EmptyMedia>
            <AlertCircleIcon className="size-8 text-destructive" />
          </EmptyMedia>
          <EmptyTitle>Failed to load usage</EmptyTitle>
          <EmptyDescription>
            {error instanceof Error ? error.message : "Unknown error"}
          </EmptyDescription>
        </EmptyHeader>
      </Empty>
    );
  }
  if (!data) return null;

  const avgPerRun =
    data.total_runs > 0 ? data.total_tokens / data.total_runs : 0;
  const ioRatio =
    data.total_input_tokens > 0
      ? data.total_output_tokens / data.total_input_tokens
      : 0;

  // Top 5 models + "other" bucket.
  const modelRows: ModelRow[] = Object.entries(data.by_model)
    .map(([model, usage]) => ({
      model,
      tokens: usage.tokens,
      runs: usage.runs,
    }))
    .sort((a, b) => b.tokens - a.tokens);
  const top = modelRows.slice(0, 5);
  const rest = modelRows.slice(5);
  if (rest.length > 0) {
    top.push({
      model: "other",
      tokens: rest.reduce((sum, r) => sum + r.tokens, 0),
      runs: rest.reduce((sum, r) => sum + r.runs, 0),
    });
  }

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <KpiCard label="Total tokens" value={formatTokenCount(data.total_tokens)} />
        <KpiCard label="Total runs" value={data.total_runs.toLocaleString()} />
        <KpiCard label="Avg tokens / run" value={formatTokenCount(Math.round(avgPerRun))} />
        <KpiCard label="Output : Input" value={`${ioRatio.toFixed(2)}×`} />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Tokens by model</CardTitle>
          <CardDescription>
            Top 5 models; remaining grouped as &quot;other&quot;.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {top.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted-foreground">
              No completed runs in this window.
            </p>
          ) : (
            <div className="h-[320px] w-full">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart
                  data={top}
                  margin={{ top: 8, right: 16, bottom: 8, left: 8 }}
                >
                  <CartesianGrid strokeDasharray="3 3" className="stroke-border" vertical={false} />
                  <XAxis
                    dataKey="model"
                    tick={{ fontSize: 11 }}
                    interval={0}
                    angle={-15}
                    textAnchor="end"
                    height={60}
                  />
                  <YAxis tick={{ fontSize: 11 }} width={56} />
                  <Tooltip
                    cursor={{ fill: "var(--muted)", fillOpacity: 0.4 }}
                    contentStyle={{
                      backgroundColor: "var(--popover)",
                      border: "1px solid var(--border)",
                      borderRadius: "var(--radius-md)",
                      color: "var(--popover-foreground)",
                      fontSize: 12,
                    }}
                    formatter={(value, _name, item) => {
                      const row = (item?.payload as ModelRow | undefined) ?? {
                        model: "",
                        tokens: 0,
                        runs: 0,
                      };
                      return [
                        `${formatTokenCount(Number(value) || 0)} tokens · ${row.runs} runs`,
                        row.model,
                      ];
                    }}
                  />
                  <Bar dataKey="tokens" radius={[4, 4, 0, 0]}>
                    {top.map((_, idx) => (
                      <Cell
                        key={idx}
                        fill={CHART_COLORS[idx % CHART_COLORS.length]}
                      />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Tokens by caller</CardTitle>
          <CardDescription>
            Lead agent vs subagent vs middleware breakdown.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ByCallerBreakdown
            lead={data.by_caller.lead_agent}
            subagent={data.by_caller.subagent}
            middleware={data.by_caller.middleware}
          />
        </CardContent>
      </Card>
    </div>
  );
}

function KpiCard({ label, value }: { label: string; value: string }) {
  return (
    <Card>
      <CardHeader>
        <CardDescription>{label}</CardDescription>
        <CardTitle className="text-2xl tabular-nums">{value}</CardTitle>
      </CardHeader>
    </Card>
  );
}

function ByCallerBreakdown({
  lead,
  subagent,
  middleware,
}: {
  lead: number;
  subagent: number;
  middleware: number;
}) {
  const total = lead + subagent + middleware || 1;
  const rows = [
    { label: "Lead agent", value: lead },
    { label: "Subagent", value: subagent },
    { label: "Middleware", value: middleware },
  ];
  return (
    <div className="space-y-3">
      {rows.map((row) => {
        const pct = (row.value / total) * 100;
        return (
          <div key={row.label} className="space-y-1">
            <div className="flex items-center justify-between text-sm">
              <span className="text-muted-foreground">{row.label}</span>
              <span className="font-mono tabular-nums">
                {formatTokenCount(row.value)} ({pct.toFixed(1)}%)
              </span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
              <div
                className="h-full rounded-full bg-primary"
                style={{ width: `${pct}%` }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function UsageSkeleton() {
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <Card key={i}>
            <CardHeader>
              <Skeleton className="h-3 w-20" />
              <Skeleton className="h-7 w-24" />
            </CardHeader>
          </Card>
        ))}
      </div>
      <Card>
        <CardHeader>
          <Skeleton className="h-5 w-40" />
        </CardHeader>
        <CardContent>
          <Skeleton className="h-[320px] w-full" />
        </CardContent>
      </Card>
    </div>
  );
}

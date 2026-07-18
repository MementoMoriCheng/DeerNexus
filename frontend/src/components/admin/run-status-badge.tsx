import type { VariantProps } from "class-variance-authority";

import { Badge, type badgeVariants } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

type BadgeVariant = NonNullable<VariantProps<typeof badgeVariants>["variant"]>;

const STATUS_VARIANT: Record<string, BadgeVariant> = {
  success: "default",
  running: "secondary",
  pending: "outline",
  error: "destructive",
  timeout: "destructive",
  interrupted: "destructive",
};

export function RunStatusBadge({ status }: { status: string }) {
  const variant = STATUS_VARIANT[status] ?? "outline";
  return (
    <Badge variant={variant} className="font-mono text-[11px]">
      {status}
    </Badge>
  );
}

/** Truncate a string for a table cell, preserving full text via title attr. */
export function TruncatedCell({
  value,
  maxLength = 24,
  className,
}: {
  value: string | null | undefined;
  maxLength?: number;
  className?: string;
}) {
  if (!value) {
    return <span className="text-muted-foreground">—</span>;
  }
  const truncated =
    value.length > maxLength ? `${value.slice(0, maxLength - 1)}…` : value;
  return (
    <span className={cn("font-mono text-xs", className)} title={value}>
      {truncated}
    </span>
  );
}

import type { VariantProps } from "class-variance-authority";

import { Badge, type badgeVariants } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

type BadgeVariant = NonNullable<VariantProps<typeof badgeVariants>["variant"]>;

/** Version status → badge variant (ADR-0004 §4 state machine). */
const VERSION_STATUS_VARIANT: Record<string, BadgeVariant> = {
  draft: "outline",
  reviewed: "secondary",
  published: "default", // success-equivalent: the only status prod admits
  revoked: "destructive",
  archived: "outline",
};

export function VersionStatusBadge({ status }: { status: string }) {
  const variant = VERSION_STATUS_VARIANT[status] ?? "outline";
  return (
    <Badge variant={variant} className="font-mono text-[11px]">
      {status}
    </Badge>
  );
}

/** Package status → badge variant (active | archived). */
const PACKAGE_STATUS_VARIANT: Record<string, BadgeVariant> = {
  active: "default",
  archived: "outline",
};

export function PackageStatusBadge({ status }: { status: string }) {
  const variant = PACKAGE_STATUS_VARIANT[status] ?? "outline";
  return (
    <Badge variant={variant} className="font-mono text-[11px]">
      {status}
    </Badge>
  );
}

/** Channel name → badge variant (dev = dev-friendly, staging = caution, prod = locked). */
const CHANNEL_VARIANT: Record<string, BadgeVariant> = {
  dev: "secondary",
  staging: "outline",
  prod: "default",
};

export function ChannelBadge({ channel }: { channel: string }) {
  const variant = CHANNEL_VARIANT[channel] ?? "outline";
  return (
    <Badge variant={variant} className="font-mono text-[11px]">
      {channel}
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

"use client";

import Link from "next/link";
import { use } from "react";

import {
  ChannelBadge,
  PackageStatusBadge,
  TruncatedCell,
  VersionStatusBadge,
} from "@/components/studio/studio-badges";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyTitle,
} from "@/components/ui/empty";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useI18n } from "@/core/i18n/hooks";
import type { ReleaseChannelName, VersionStatus } from "@/core/studio";
import {
  STUDIO_PERM,
  useArchivePackage,
  usePromoteChannel,
  usePublishVersion,
  useReconcileInventory,
  useReviewVersion,
  useRevokeVersion,
  useRollbackChannel,
  useStudioButtonProps,
  useStudioChannels,
  useStudioChannelEvents,
  useStudioPackage,
  useStudioPermission,
  useStudioVersions,
} from "@/core/studio";

const CHANNELS: ReleaseChannelName[] = ["dev", "staging", "prod"];

export default function StudioPackageDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id: packageId } = use(params);
  const { t } = useI18n();

  return (
    <div className="space-y-6">
      <PackageHeader packageId={packageId} />
      <Tabs defaultValue="versions">
        <TabsList>
          <TabsTrigger value="versions">
            {t.studio.detail.tabs.versions}
          </TabsTrigger>
          <TabsTrigger value="channels">
            {t.studio.detail.tabs.channels}
          </TabsTrigger>
          <TabsTrigger value="overview">
            {t.studio.detail.tabs.overview}
          </TabsTrigger>
        </TabsList>
        <TabsContent value="versions" className="mt-4">
          <VersionsTab packageId={packageId} />
        </TabsContent>
        <TabsContent value="channels" className="mt-4">
          <ChannelsTab packageId={packageId} />
        </TabsContent>
        <TabsContent value="overview" className="mt-4">
          <OverviewTab packageId={packageId} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

function PackageHeader({ packageId }: { packageId: string }) {
  const { t } = useI18n();
  const { data: pkg, isLoading, isError, error } = useStudioPackage(packageId);
  if (isLoading) return <Skeleton className="h-10 w-full" />;
  if (isError || !pkg) {
    return (
      <p className="text-destructive text-sm">
        {error instanceof Error ? error.message : t.studio.detail.loadErrorPackage}
      </p>
    );
  }
  return (
    <div className="flex items-start justify-between gap-4">
      <div>
        <div className="flex items-center gap-2">
          <h1 className="text-2xl font-semibold tracking-tight">
            {pkg.display_name}
          </h1>
          <PackageStatusBadge status={pkg.status} />
        </div>
        <p className="text-muted-foreground font-mono text-sm">{pkg.name}</p>
      </div>
    </div>
  );
}

// ── Versions tab ──────────────────────────────────────────────────────

function VersionsTab({ packageId }: { packageId: string }) {
  const { t } = useI18n();
  const {
    data: versions,
    isLoading,
    isError,
    error,
  } = useStudioVersions(packageId);
  const writePerm = useStudioButtonProps(STUDIO_PERM.packageWrite);

  const newVersionButton = (
    <Link href={`/studio/packages/${packageId}/new-version`}>
      <Button size="sm" disabled={writePerm.disabled} title={writePerm.title}>
        {t.studio.detail.newVersion}
      </Button>
    </Link>
  );

  if (isLoading) return <Skeleton className="h-32 w-full" />;
  if (isError) {
    return (
      <p className="text-destructive text-sm">
        {error instanceof Error
          ? error.message
          : t.studio.detail.loadErrorVersions}
      </p>
    );
  }
  if (!versions || versions.length === 0) {
    return (
      <div className="space-y-4">
        <div className="flex justify-end">{newVersionButton}</div>
        <Empty>
          <EmptyHeader>
            <EmptyTitle>{t.studio.detail.versionEmptyTitle}</EmptyTitle>
            <EmptyDescription>
              {t.studio.detail.versionEmptyDescription}
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      </div>
    );
  }
  return (
    <div className="space-y-4">
      <div className="flex justify-end">{newVersionButton}</div>
      <Card>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t.studio.detail.versionColumns.version}</TableHead>
              <TableHead>{t.studio.detail.versionColumns.digest}</TableHead>
              <TableHead>{t.studio.detail.versionColumns.status}</TableHead>
              <TableHead>{t.studio.detail.versionColumns.size}</TableHead>
              <TableHead>{t.studio.detail.versionColumns.created}</TableHead>
              <TableHead className="text-right">
                {t.studio.detail.versionColumns.actions}
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {versions.map((v) => (
              <TableRow key={v.id}>
                <TableCell className="font-mono text-sm">{v.version}</TableCell>
                <TableCell>
                  <TruncatedCell value={v.digest} maxLength={20} />
                </TableCell>
                <TableCell>
                  <VersionStatusBadge status={v.status} />
                </TableCell>
                <TableCell className="text-muted-foreground text-xs tabular-nums">
                  {formatBytes(v.size_bytes)}
                </TableCell>
                <TableCell className="text-muted-foreground text-xs tabular-nums">
                  {new Date(v.created_at).toLocaleDateString()}
                </TableCell>
                <TableCell className="text-right">
                  <VersionActions versionId={v.id} status={v.status} />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Card>
    </div>
  );
}

/** Lifecycle action buttons, gated on the current status (ADR §4 state machine)
 * and the caller's studio:package:write permission (PR-057 follow-up). */
function VersionActions({
  versionId,
  status,
}: {
  versionId: string;
  status: VersionStatus;
}) {
  const { t } = useI18n();
  const review = useReviewVersion();
  const publish = usePublishVersion();
  const revoke = useRevokeVersion();
  // review/publish/revoke all require studio:package:write (org:admin + org:developer).
  const perm = useStudioButtonProps(STUDIO_PERM.packageWrite);
  const disabled = (pending: boolean) => pending || perm.disabled;
  return (
    <div className="flex justify-end gap-1.5">
      {status === "draft" && (
        <Button
          size="sm"
          variant="outline"
          disabled={disabled(review.isPending)}
          title={perm.title}
          onClick={() => review.mutate(versionId)}
        >
          {review.isPending
            ? t.studio.detail.actions.reviewing
            : t.studio.detail.actions.review}
        </Button>
      )}
      {status === "reviewed" && (
        <Button
          size="sm"
          variant="outline"
          disabled={disabled(publish.isPending)}
          title={perm.title}
          onClick={() => publish.mutate(versionId)}
        >
          {publish.isPending
            ? t.studio.detail.actions.publishing
            : t.studio.detail.actions.publish}
        </Button>
      )}
      {status === "published" && (
        <Button
          size="sm"
          variant="outline"
          disabled={disabled(revoke.isPending)}
          title={perm.title}
          onClick={() => revoke.mutate(versionId)}
        >
          {revoke.isPending
            ? t.studio.detail.actions.revoking
            : t.studio.detail.actions.revoke}
        </Button>
      )}
    </div>
  );
}

// ── Channels tab ──────────────────────────────────────────────────────

function ChannelsTab({ packageId }: { packageId: string }) {
  const { t } = useI18n();
  const {
    data: channels,
    isLoading,
    isError,
    error,
  } = useStudioChannels(packageId);
  const { data: versions } = useStudioVersions(packageId);

  if (isLoading) return <Skeleton className="h-32 w-full" />;
  if (isError) {
    return (
      <p className="text-destructive text-sm">
        {error instanceof Error
          ? error.message
          : t.studio.detail.loadErrorChannels}
      </p>
    );
  }
  if (!channels) return null;

  return (
    <div className="space-y-4">
      {CHANNELS.map((channelName) => {
        const channel = channels.find((c) => c.channel === channelName) ?? null;
        return (
          <ChannelCard
            key={channelName}
            packageId={packageId}
            channelName={channelName}
            channel={channel}
            versions={versions ?? []}
          />
        );
      })}
    </div>
  );
}

function ChannelCard({
  packageId,
  channelName,
  channel,
  versions,
}: {
  packageId: string;
  channelName: ReleaseChannelName;
  channel: { current_version_id: string | null; row_version: number } | null;
  versions: { id: string; version: string; status: string }[];
}) {
  // A channel row may not exist yet (NULL until first promote). Treat absent
  // as an empty pointer; the events query only runs once the channel row exists.
  const { t } = useI18n();
  const currentVersionId = channel?.current_version_id ?? null;
  const rowVersion = channel?.row_version ?? 0;
  const { data: events } = useStudioChannelEvents(
    packageId,
    currentVersionId === null && rowVersion === 0 ? null : channelName,
  );
  const promote = usePromoteChannel();
  const rollback = useRollbackChannel();

  // Permission gating (PR-057 follow-up): dev accepts promote_dev (org:developer+),
  // staging/prod require promote (org:admin only); rollback requires rollback (admin).
  const canPromote = useStudioPermission(
    channelName === "dev" ? STUDIO_PERM.promoteDev : STUDIO_PERM.promote,
  );
  const canRollback = useStudioPermission(STUDIO_PERM.rollback);
  const promotePermTitle =
    channelName === "dev"
      ? t.studio.detail.promotePermTitleDev(STUDIO_PERM.promoteDev)
      : t.studio.detail.promotePermTitle(STUDIO_PERM.promote);
  const rollbackPermTitle = t.studio.detail.rollbackPermTitle(
    STUDIO_PERM.rollback,
  );

  // Versions eligible to promote onto this channel (published for prod, broader for dev/staging).
  const promotableVersions = versions.filter(
    (v) => v.status !== "revoked" && v.status !== "archived",
  );
  const currentVersion = versions.find((v) => v.id === currentVersionId);

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <CardTitle className="text-base">
            <ChannelBadge channel={channelName} /> {channelName}
          </CardTitle>
          <span className="text-muted-foreground text-xs">
            row_version {rowVersion}
          </span>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm">
          <span className="text-muted-foreground">
            {t.studio.detail.currentLabel}
          </span>
          {currentVersion ? (
            <span className="font-mono">{currentVersion.version}</span>
          ) : (
            <span className="text-muted-foreground italic">
              {t.studio.detail.emptyPointer}
            </span>
          )}
        </p>
        <div className="flex flex-wrap gap-2">
          <ChannelMoveSelect
            label={t.studio.detail.promoteLabel}
            toLabel={t.studio.detail.toLabel}
            selectPlaceholder={t.studio.detail.selectVersionPlaceholder}
            disabled={
              promote.isPending ||
              promotableVersions.length === 0 ||
              !canPromote
            }
            disabledTitle={canPromote ? "" : promotePermTitle}
            versions={promotableVersions}
            expectedChannelVersion={rowVersion}
            onSubmit={(targetVersionId, expectedChannelVersion) =>
              promote.mutate({
                packageId,
                channel: channelName,
                request: {
                  target_version_id: targetVersionId,
                  expected_channel_version: expectedChannelVersion,
                },
              })
            }
          />
          <ChannelMoveSelect
            label={t.studio.detail.rollbackLabel}
            toLabel={t.studio.detail.toLabel}
            selectPlaceholder={t.studio.detail.selectVersionPlaceholder}
            disabled={
              rollback.isPending ||
              promotableVersions.length === 0 ||
              !canRollback
            }
            disabledTitle={canRollback ? "" : rollbackPermTitle}
            versions={promotableVersions}
            expectedChannelVersion={rowVersion}
            onSubmit={(targetVersionId, expectedChannelVersion) =>
              rollback.mutate({
                packageId,
                channel: channelName,
                request: {
                  target_version_id: targetVersionId,
                  expected_channel_version: expectedChannelVersion,
                },
              })
            }
          />
        </div>
        {events && events.length > 0 && (
          <div className="border-t pt-3">
            <p className="text-muted-foreground mb-2 text-xs font-medium tracking-wide uppercase">
              {t.studio.detail.historyLabel}
            </p>
            <div className="space-y-1">
              {events.slice(0, 5).map((e) => (
                <div
                  key={e.id}
                  className="text-muted-foreground flex items-center gap-2 text-xs"
                >
                  <span className="font-mono">{e.action}</span>
                  <span>
                    {t.studio.detail.byLabel}{" "}
                    {e.actor_id ?? t.studio.detail.systemActor}
                  </span>
                  <span className="tabular-nums">
                    {new Date(e.created_at).toLocaleString()}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/** A small inline select+button to pick a target version and submit a CAS move. */
function ChannelMoveSelect({
  label,
  toLabel,
  selectPlaceholder,
  disabled,
  disabledTitle,
  versions,
  expectedChannelVersion,
  onSubmit,
}: {
  label: string;
  toLabel: string;
  selectPlaceholder: string;
  disabled: boolean;
  disabledTitle?: string;
  versions: { id: string; version: string; status: string }[];
  expectedChannelVersion?: number;
  onSubmit: (targetVersionId: string, expectedChannelVersion: number) => void;
}) {
  return (
    <form
      className="flex items-end gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        const form = e.currentTarget;
        const select = form.elements.namedItem(
          "target",
        ) as HTMLSelectElement | null;
        const target = select?.value;
        if (target && expectedChannelVersion !== undefined) {
          onSubmit(target, expectedChannelVersion);
        }
      }}
    >
      <label className="text-muted-foreground flex flex-col gap-1 text-xs">
        {label}
        {toLabel}
        <select
          name="target"
          disabled={disabled}
          title={disabledTitle}
          className="bg-input border-input ring-offset-background focus-visible:ring-ring h-8 rounded-md border px-2 text-xs focus-visible:ring-2 focus-visible:outline-none"
          defaultValue=""
        >
          <option value="" disabled>
            {selectPlaceholder}
          </option>
          {versions.map((v) => (
            <option key={v.id} value={v.id}>
              {v.version} ({v.status})
            </option>
          ))}
        </select>
      </label>
      <Button
        type="submit"
        size="sm"
        variant="outline"
        disabled={disabled}
        title={disabledTitle}
      >
        {label}
      </Button>
    </form>
  );
}

// ── Overview tab ──────────────────────────────────────────────────────

function OverviewTab({ packageId }: { packageId: string }) {
  const { t } = useI18n();
  const { data: pkg, isLoading, isError, error } = useStudioPackage(packageId);
  const archive = useArchivePackage();
  const reconcile = useReconcileInventory();
  // archive + reconcile require studio:package:write (org:admin + org:developer).
  const writePerm = useStudioButtonProps(STUDIO_PERM.packageWrite);

  if (isLoading) return <Skeleton className="h-48 w-full" />;
  if (isError || !pkg) {
    return (
      <p className="text-destructive text-sm">
        {error instanceof Error ? error.message : t.studio.detail.loadErrorPackage}
      </p>
    );
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t.studio.detail.metaTitle}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <MetaRow label={t.studio.detail.meta.id} value={pkg.id} mono />
        <MetaRow label={t.studio.detail.meta.name} value={pkg.name} mono />
        <MetaRow
          label={t.studio.detail.meta.displayName}
          value={pkg.display_name}
        />
        <MetaRow
          label={t.studio.detail.meta.description}
          value={pkg.description ?? "—"}
        />
        <MetaRow label={t.studio.detail.meta.status} value={pkg.status} />
        <MetaRow
          label={t.studio.detail.meta.workspace}
          value={pkg.workspace_id ?? "—"}
          mono
        />
        <MetaRow
          label={t.studio.detail.meta.createdBy}
          value={pkg.created_by ?? "—"}
          mono
        />
        <MetaRow
          label={t.studio.detail.meta.createdAt}
          value={new Date(pkg.created_at).toLocaleString()}
        />
        <MetaRow
          label={t.studio.detail.meta.updatedAt}
          value={new Date(pkg.updated_at).toLocaleString()}
        />
        <div className="flex flex-wrap gap-2 border-t pt-3">
          <Button
            size="sm"
            variant="outline"
            disabled={reconcile.isPending || writePerm.disabled}
            title={writePerm.title}
            onClick={() => reconcile.mutate()}
          >
            {reconcile.isPending
              ? t.studio.detail.reconciling
              : t.studio.detail.reconcile}
          </Button>
          {pkg.status === "active" && (
            <Button
              size="sm"
              variant="outline"
              disabled={archive.isPending || writePerm.disabled}
              title={writePerm.title}
              onClick={() => archive.mutate(packageId)}
            >
              {archive.isPending
                ? t.studio.detail.archiving
                : t.studio.detail.archive}
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function MetaRow({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="flex gap-2 text-sm">
      <span className="text-muted-foreground w-32 shrink-0">{label}</span>
      <span className={mono ? "font-mono text-xs" : ""}>{value}</span>
    </div>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

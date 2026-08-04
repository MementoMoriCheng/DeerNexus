"use client";

import Link from "next/link";

import { PackageStatusBadge } from "@/components/studio/studio-badges";
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
import { useI18n } from "@/core/i18n/hooks";
import { useStudioPackages } from "@/core/studio";

export default function StudioPackagesPage() {
  const { t } = useI18n();
  const { data: packages, isLoading, isError, error } = useStudioPackages();

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            {t.studio.packages.title}
          </h1>
          <p className="text-muted-foreground text-sm">
            {t.studio.packages.description}
          </p>
        </div>
        <Button asChild variant="outline" size="sm">
          <Link href="/studio/import">{t.studio.packages.importAgent}</Link>
        </Button>
      </div>

      {isError ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-destructive text-base">
              {t.studio.packages.loadError}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-muted-foreground text-sm">
              {error instanceof Error
                ? error.message
                : t.studio.packages.loadErrorFallback}
            </p>
          </CardContent>
        </Card>
      ) : isLoading ? (
        <PackagesSkeleton />
      ) : !packages || packages.length === 0 ? (
        <Empty>
          <EmptyHeader>
            <EmptyTitle>{t.studio.packages.emptyTitle}</EmptyTitle>
            <EmptyDescription>
              {t.studio.packages.emptyDescription}
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t.studio.packages.columns.name}</TableHead>
                <TableHead>{t.studio.packages.columns.displayName}</TableHead>
                <TableHead>{t.studio.packages.columns.status}</TableHead>
                <TableHead>{t.studio.packages.columns.created}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {packages.map((pkg) => (
                <TableRow key={pkg.id}>
                  <TableCell>
                    <Link
                      href={`/studio/packages/${pkg.id}`}
                      className="hover:text-primary font-mono text-sm underline-offset-2 hover:underline"
                    >
                      {pkg.name}
                    </Link>
                  </TableCell>
                  <TableCell className="text-sm">{pkg.display_name}</TableCell>
                  <TableCell>
                    <PackageStatusBadge status={pkg.status} />
                  </TableCell>
                  <TableCell className="text-muted-foreground text-xs tabular-nums">
                    {new Date(pkg.created_at).toLocaleString()}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}
    </div>
  );
}

function PackagesSkeleton() {
  return (
    <Card>
      <div className="space-y-3 p-6">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-8 w-full" />
        ))}
      </div>
    </Card>
  );
}

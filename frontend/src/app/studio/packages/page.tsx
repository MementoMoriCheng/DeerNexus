"use client";

import Link from "next/link";

import { PackageStatusBadge } from "@/components/studio/studio-badges";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useStudioPackages } from "@/core/studio";

export default function StudioPackagesPage() {
  const { data: packages, isLoading, isError, error } = useStudioPackages();

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Agent Packages</h1>
          <p className="text-muted-foreground text-sm">
            Manage agent artifacts, versions, and release channels.
          </p>
        </div>
        <Button asChild variant="outline" size="sm">
          <Link href="/studio/import">Import agent</Link>
        </Button>
      </div>

      {isError ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-destructive text-base">
              Failed to load packages
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-muted-foreground text-sm">
              {error instanceof Error
                ? error.message
                : "The gateway may be unreachable, or you may lack studio permission."}
            </p>
          </CardContent>
        </Card>
      ) : isLoading ? (
        <PackagesSkeleton />
      ) : !packages || packages.length === 0 ? (
        <Empty>
          <EmptyHeader>
            <EmptyTitle>No agent packages yet</EmptyTitle>
            <EmptyDescription>
              Import an agent from the file-state layout to create its first
              package and version.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Display name</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Created</TableHead>
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

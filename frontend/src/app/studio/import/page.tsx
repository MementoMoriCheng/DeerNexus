"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useImportAgent, useStudioPermission } from "@/core/studio";
import { STUDIO_PERM } from "@/core/studio";
import type { ImportReport } from "@/core/studio";

export default function StudioImportPage() {
  const importMutation = useImportAgent();
  const canWrite = useStudioPermission(STUDIO_PERM.packageWrite);
  const [name, setName] = useState("");
  const [version, setVersion] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [userId, setUserId] = useState("");

  const canSubmit =
    canWrite &&
    name.trim() !== "" &&
    version.trim() !== "" &&
    !importMutation.isPending;

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    importMutation.mutate({
      name: name.trim(),
      version: version.trim(),
      display_name: displayName.trim() || undefined,
      description: description.trim() || undefined,
      user_id: userId.trim() || null,
    });
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Import agent</h1>
        <p className="text-muted-foreground text-sm">
          Import an agent from the file-state layout (SOUL / config). The
          importer computes a digest and is idempotent: re-importing identical
          content returns the existing version instead of duplicating.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">File-state import</CardTitle>
          <CardDescription>
            Reads{" "}
            <code className="font-mono text-xs">
              agents/&lbrace;name&rbrace;/
            </code>{" "}
            from the server-side agent directory. Requires the{" "}
            <code className="font-mono text-xs">studio:package:write</code>{" "}
            permission.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {!canWrite && (
            <p className="text-muted-foreground mb-4 rounded-md border border-dashed px-3 py-2 text-xs">
              You need the{" "}
              <code className="font-mono">studio:package:write</code> permission
              (org:admin or org:developer) to import agents. The form below is
              disabled.
            </p>
          )}
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-2">
              <label htmlFor="name">Agent directory name *</label>
              <Input
                id="name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="my-agent"
                pattern="[A-Za-z0-9-]+"
                title="Letters, digits, and hyphens only"
                required
              />
            </div>
            <div className="space-y-2">
              <label htmlFor="version">Version (SemVer) *</label>
              <Input
                id="version"
                value={version}
                onChange={(e) => setVersion(e.target.value)}
                placeholder="1.0.0"
                required
              />
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <label htmlFor="display-name">Display name</label>
                <Input
                  id="display-name"
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                  placeholder="defaults to name"
                />
              </div>
              <div className="space-y-2">
                <label htmlFor="user-id">User ID (optional)</label>
                <Input
                  id="user-id"
                  value={userId}
                  onChange={(e) => setUserId(e.target.value)}
                  placeholder="per-user agent dir"
                />
              </div>
            </div>
            <div className="space-y-2">
              <label htmlFor="description">Description</label>
              <Textarea
                id="description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="defaults to the agent config description"
                rows={3}
              />
            </div>
            <Button type="submit" disabled={!canSubmit}>
              {importMutation.isPending ? "Importing…" : "Import agent"}
            </Button>
          </form>
        </CardContent>
      </Card>

      {importMutation.data && <ImportReportCard report={importMutation.data} />}
    </div>
  );
}

function ImportReportCard({ report }: { report: ImportReport }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          {report.imported ? "Imported" : "Idempotent re-import"}
        </CardTitle>
        <CardDescription>
          {report.imported
            ? "A new package + version were created."
            : "Identical content already imported — existing version returned."}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-1.5">
        <MetaRow label="Package" value={report.package.name} mono />
        <MetaRow label="Version" value={report.version.version} mono />
        <MetaRow label="Status" value={report.version.status} mono />
        <MetaRow label="Digest" value={report.digest} mono />
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
      <span className="text-muted-foreground w-24 shrink-0">{label}</span>
      <span className={mono ? "font-mono text-xs break-all" : ""}>{value}</span>
    </div>
  );
}

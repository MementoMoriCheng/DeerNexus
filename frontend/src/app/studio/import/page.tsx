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
import { useI18n } from "@/core/i18n/hooks";
import { useImportAgent, useStudioPermission } from "@/core/studio";
import { STUDIO_PERM } from "@/core/studio";
import type { ImportReport } from "@/core/studio";

export default function StudioImportPage() {
  const { t } = useI18n();
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
        <h1 className="text-2xl font-semibold tracking-tight">
          {t.studio.importPage.title}
        </h1>
        <p className="text-muted-foreground text-sm">
          {t.studio.importPage.description}
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            {t.studio.importPage.methodTitle}
          </CardTitle>
          <CardDescription>
            {t.studio.importPage.methodDescription}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {!canWrite && (
            <p className="text-muted-foreground mb-4 rounded-md border border-dashed px-3 py-2 text-xs">
              {t.studio.importPage.permissionHint}
            </p>
          )}
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-2">
              <label htmlFor="name">
                {t.studio.importPage.labels.agentDirName}
              </label>
              <Input
                id="name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="my-agent"
                pattern="[A-Za-z0-9-]+"
                title={t.studio.importPage.labels.agentDirNameTitle}
                required
              />
            </div>
            <div className="space-y-2">
              <label htmlFor="version">
                {t.studio.importPage.labels.version}
              </label>
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
                <label htmlFor="display-name">
                  {t.studio.importPage.labels.displayName}
                </label>
                <Input
                  id="display-name"
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                  placeholder={
                    t.studio.importPage.labels.displayNamePlaceholder
                  }
                />
              </div>
              <div className="space-y-2">
                <label htmlFor="user-id">
                  {t.studio.importPage.labels.userId}
                </label>
                <Input
                  id="user-id"
                  value={userId}
                  onChange={(e) => setUserId(e.target.value)}
                  placeholder={t.studio.importPage.labels.userIdPlaceholder}
                />
              </div>
            </div>
            <div className="space-y-2">
              <label htmlFor="description">
                {t.studio.importPage.labels.description}
              </label>
              <Textarea
                id="description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder={t.studio.importPage.labels.descriptionPlaceholder}
                rows={3}
              />
            </div>
            <Button type="submit" disabled={!canSubmit}>
              {importMutation.isPending
                ? t.studio.importPage.importing
                : t.studio.importPage.submit}
            </Button>
          </form>
        </CardContent>
      </Card>

      {importMutation.data && <ImportReportCard report={importMutation.data} />}
    </div>
  );
}

function ImportReportCard({ report }: { report: ImportReport }) {
  const { t } = useI18n();
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          {report.imported
            ? t.studio.importPage.successImported
            : t.studio.importPage.successIdempotent}
        </CardTitle>
        <CardDescription>
          {report.imported
            ? t.studio.importPage.successImportedDesc
            : t.studio.importPage.successIdempotentDesc}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-1.5">
        <MetaRow
          label={t.studio.importPage.meta.package}
          value={report.package.name}
          mono
        />
        <MetaRow
          label={t.studio.importPage.meta.version}
          value={report.version.version}
          mono
        />
        <MetaRow
          label={t.studio.importPage.meta.status}
          value={report.version.status}
          mono
        />
        <MetaRow
          label={t.studio.importPage.meta.digest}
          value={report.digest}
          mono
        />
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

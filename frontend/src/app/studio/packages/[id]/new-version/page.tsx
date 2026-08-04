"use client";

import { useRouter } from "next/navigation";
import { use, useState } from "react";

import { DynamicListRow } from "@/components/studio/dynamic-list-row";
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
import { useCreateVersion, useStudioPermission } from "@/core/studio";
import { STUDIO_PERM } from "@/core/studio";
import type {
  AgentManifest,
  CreateVersionRequest,
  DependencyLock,
  McpServerRef,
  ModelRequirement,
  NetworkRequirement,
  SecretRequirement,
  SkillRef,
} from "@/core/studio";

/** SemVer 2.0.0 shape (mirrors backend _SEMVER_RE). */
const SEMVER_RE =
  /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$/;

export default function NewVersionPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id: packageId } = use(params);
  const router = useRouter();
  const { t } = useI18n();
  const createVersion = useCreateVersion();
  const canWrite = useStudioPermission(STUDIO_PERM.packageWrite);

  // ── Form state ──
  const [version, setVersion] = useState("");
  const [content, setContent] = useState("");
  const [schemaVersion, setSchemaVersion] = useState("v1alpha1");
  const [agentEntry, setAgentEntry] = useState("");
  const [soulOrPromptRef, setSoulOrPromptRef] = useState("");

  const [modelRequirements, setModelRequirements] = useState<
    ModelRequirement[]
  >([]);
  const [skills, setSkills] = useState<SkillRef[]>([]);
  const [tools, setTools] = useState<string[]>([]);
  const [mcpServers, setMcpServers] = useState<McpServerRef[]>([]);
  const [dependencies, setDependencies] = useState<DependencyLock[]>([]);
  const [networkRequirements, setNetworkRequirements] = useState<
    NetworkRequirement[]
  >([]);
  const [secretRequirements, setSecretRequirements] = useState<
    SecretRequirement[]
  >([]);

  const [maxSteps, setMaxSteps] = useState("");
  const [maxTokens, setMaxTokens] = useState("");
  const [timeoutS, setTimeoutS] = useState("");

  const versionValid = SEMVER_RE.test(version.trim());
  const canSubmit =
    canWrite &&
    versionValid &&
    agentEntry.trim() !== "" &&
    content.trim() !== "" &&
    !createVersion.isPending;

  function buildRequest(): CreateVersionRequest {
    const manifest: AgentManifest = {
      schema_version: schemaVersion.trim() || "v1alpha1",
      agent_entry: agentEntry.trim(),
      soul_or_prompt_ref: soulOrPromptRef.trim() || null,
      model_requirements: modelRequirements.filter((r) => r.name.trim()),
      skills: skills.filter((s) => s.name.trim()),
      tools: tools.filter((t) => t.trim()),
      mcp_servers: mcpServers.filter((m) => m.name.trim()),
      dependencies: dependencies.filter((d) => d.name.trim()),
      network_requirements: networkRequirements.filter((n) => n.host.trim()),
      secret_requirements: secretRequirements.filter(
        (s) => s.name.trim() && s.ref.trim(),
      ),
      runtime_limits: {},
    };
    // Only populate runtime_limits if any field is set.
    const limits = manifest.runtime_limits!;
    if (maxSteps.trim()) limits.max_steps = Number(maxSteps);
    if (maxTokens.trim()) limits.max_tokens = Number(maxTokens);
    if (timeoutS.trim()) limits.timeout_s = Number(timeoutS);
    if (Object.keys(limits).length === 0) manifest.runtime_limits = null;

    return { version: version.trim(), manifest, content };
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    createVersion.mutate(
      { packageId, request: buildRequest() },
      { onSuccess: () => router.push(`/studio/packages/${packageId}`) },
    );
  }

  if (!canWrite) {
    return (
      <div className="mx-auto max-w-2xl space-y-6">
        <h1 className="text-2xl font-semibold tracking-tight">
          {t.studio.newVersion.title}
        </h1>
        <p className="text-muted-foreground rounded-md border border-dashed px-3 py-2 text-xs">
          {t.studio.newVersion.permissionHint}
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">
          {t.studio.newVersion.title}
        </h1>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => router.push(`/studio/packages/${packageId}`)}
        >
          ← {t.studio.newVersion.back}
        </Button>
      </div>

      <form onSubmit={handleSubmit} className="space-y-6">
        {/* ── Basics (required) ── */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              {t.studio.newVersion.basics}
            </CardTitle>
            <CardDescription>
              {t.studio.newVersion.basicsDescription}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
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
              {version && !versionValid && (
                <p className="text-destructive text-xs">
                  {t.studio.newVersion.semverError}
                </p>
              )}
            </div>
            <div className="space-y-2">
              <label htmlFor="content">
                {t.studio.newVersion.contentLabel}
              </label>
              <Textarea
                id="content"
                value={content}
                onChange={(e) => setContent(e.target.value)}
                placeholder={t.studio.newVersion.contentPlaceholder}
                rows={8}
                required
              />
            </div>
          </CardContent>
        </Card>

        {/* ── Manifest core (required) ── */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              {t.studio.newVersion.manifestCore}
            </CardTitle>
            <CardDescription>
              {t.studio.newVersion.manifestCoreDescription}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <label htmlFor="schema-version">
                  {t.studio.newVersion.schemaVersion}
                </label>
                <Input
                  id="schema-version"
                  value={schemaVersion}
                  onChange={(e) => setSchemaVersion(e.target.value)}
                />
              </div>
              <div className="space-y-2">
                <label htmlFor="agent-entry">
                  {t.studio.newVersion.agentEntry}
                </label>
                <Input
                  id="agent-entry"
                  value={agentEntry}
                  onChange={(e) => setAgentEntry(e.target.value)}
                  placeholder={t.studio.newVersion.agentEntryPlaceholder}
                  required
                />
              </div>
            </div>
            <div className="space-y-2">
              <label htmlFor="soul-prompt">
                {t.studio.newVersion.soulPrompt}
              </label>
              <Textarea
                id="soul-prompt"
                value={soulOrPromptRef}
                onChange={(e) => setSoulOrPromptRef(e.target.value)}
                placeholder={t.studio.newVersion.soulPromptPlaceholder}
                rows={3}
              />
            </div>
          </CardContent>
        </Card>

        {/* ── Model requirements ── */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              {t.studio.newVersion.modelRequirements}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <DynamicListRow<ModelRequirement>
              fields={[{ key: "name", label: "model name", required: true }]}
              value={modelRequirements}
              onChange={setModelRequirements}
              addLabel={t.studio.newVersion.addLabels.model}
              createRow={() => ({ name: "" })}
            />
          </CardContent>
        </Card>

        {/* ── Skills ── */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              {t.studio.newVersion.skills}
            </CardTitle>
            <CardDescription>
              {t.studio.newVersion.skillsDescription}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <DynamicListRow<SkillRef>
              fields={[
                { key: "name", label: "skill name", required: true },
                { key: "version", label: "version" },
                { key: "digest", label: "digest" },
              ]}
              value={skills}
              onChange={setSkills}
              addLabel={t.studio.newVersion.addLabels.skill}
              createRow={() => ({ name: "" })}
            />
          </CardContent>
        </Card>

        {/* ── Tools ── */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              {t.studio.newVersion.tools}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <DynamicListRow<string>
              fields={[]}
              value={tools}
              onChange={setTools}
              addLabel={t.studio.newVersion.addLabels.tool}
              createRow={() => ""}
            />
          </CardContent>
        </Card>

        {/* ── MCP servers ── */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              {t.studio.newVersion.mcpServers}
            </CardTitle>
            <CardDescription>
              {t.studio.newVersion.mcpServersDescription}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <DynamicListRow<McpServerRef>
              fields={[
                { key: "name", label: "server name", required: true },
                { key: "version", label: "version" },
              ]}
              value={mcpServers}
              onChange={setMcpServers}
              addLabel={t.studio.newVersion.addLabels.mcp}
              createRow={() => ({ name: "" })}
            />
          </CardContent>
        </Card>

        {/* ── Dependencies ── */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              {t.studio.newVersion.dependencies}
            </CardTitle>
            <CardDescription>
              {t.studio.newVersion.dependenciesDescription}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <DynamicListRow<DependencyLock>
              fields={[
                { key: "name", label: "dependency name", required: true },
                { key: "version", label: "version" },
                { key: "source", label: "source" },
              ]}
              value={dependencies}
              onChange={setDependencies}
              addLabel={t.studio.newVersion.addLabels.dependency}
              createRow={() => ({ name: "" })}
            />
          </CardContent>
        </Card>

        {/* ── Network requirements ── */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              {t.studio.newVersion.networkRequirements}
            </CardTitle>
            <CardDescription>
              {t.studio.newVersion.networkRequirementsDescription}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <DynamicListRow<NetworkRequirement>
              fields={[
                { key: "host", label: "host", required: true },
                { key: "port", label: "port", type: "number" },
                { key: "protocol", label: "protocol" },
              ]}
              value={networkRequirements}
              onChange={setNetworkRequirements}
              addLabel={t.studio.newVersion.addLabels.network}
              createRow={() => ({ host: "" })}
            />
          </CardContent>
        </Card>

        {/* ── Secret requirements ── */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              {t.studio.newVersion.secretRequirements}
            </CardTitle>
            <CardDescription>
              {t.studio.newVersion.secretRequirementsDescription}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <DynamicListRow<SecretRequirement>
              fields={[
                { key: "name", label: "name", required: true },
                { key: "ref", label: "secret ref", required: true },
              ]}
              value={secretRequirements}
              onChange={setSecretRequirements}
              addLabel={t.studio.newVersion.addLabels.secret}
              createRow={() => ({ name: "", ref: "" })}
            />
          </CardContent>
        </Card>

        {/* ── Runtime limits ── */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              {t.studio.newVersion.runtimeLimits}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-3 gap-4">
              <div className="space-y-2">
                <label htmlFor="max-steps">
                  {t.studio.newVersion.maxSteps}
                </label>
                <Input
                  id="max-steps"
                  type="number"
                  value={maxSteps}
                  onChange={(e) => setMaxSteps(e.target.value)}
                  placeholder="—"
                />
              </div>
              <div className="space-y-2">
                <label htmlFor="max-tokens">
                  {t.studio.newVersion.maxTokens}
                </label>
                <Input
                  id="max-tokens"
                  type="number"
                  value={maxTokens}
                  onChange={(e) => setMaxTokens(e.target.value)}
                  placeholder="—"
                />
              </div>
              <div className="space-y-2">
                <label htmlFor="timeout">{t.studio.newVersion.timeout}</label>
                <Input
                  id="timeout"
                  type="number"
                  value={timeoutS}
                  onChange={(e) => setTimeoutS(e.target.value)}
                  placeholder="—"
                />
              </div>
            </div>
          </CardContent>
        </Card>

        <div className="flex justify-end gap-2">
          <Button
            type="button"
            variant="outline"
            onClick={() => router.push(`/studio/packages/${packageId}`)}
          >
            {t.studio.newVersion.cancel}
          </Button>
          <Button type="submit" disabled={!canSubmit}>
            {createVersion.isPending
              ? t.studio.newVersion.creating
              : t.studio.newVersion.submit}
          </Button>
        </div>
      </form>
    </div>
  );
}

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
        <h1 className="text-2xl font-semibold tracking-tight">New version</h1>
        <p className="text-muted-foreground rounded-md border border-dashed px-3 py-2 text-xs">
          You need the <code className="font-mono">studio:package:write</code>{" "}
          permission (org:admin or org:developer) to create versions.
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">New version</h1>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => router.push(`/studio/packages/${packageId}`)}
        >
          ← Back
        </Button>
      </div>

      <form onSubmit={handleSubmit} className="space-y-6">
        {/* ── Basics (required) ── */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Basics</CardTitle>
            <CardDescription>
              Version (SemVer 2.0) and artifact content. The backend computes
              the digest over the content&apos;s UTF-8 bytes.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <label htmlFor="version">Version (SemVer) *</label>
              <Input
                id="version"
                value={version}
                onChange={(e) => setVersion(e.target.value)}
                placeholder="1.0.0"
                required
              />
              {version && !versionValid && (
                <p className="text-destructive text-xs">
                  Must be a valid SemVer 2.0.0 string (e.g. 1.0.0, 1.0.0-beta).
                </p>
              )}
            </div>
            <div className="space-y-2">
              <label htmlFor="content">Artifact content (UTF-8) *</label>
              <Textarea
                id="content"
                value={content}
                onChange={(e) => setContent(e.target.value)}
                placeholder="Raw artifact payload — the agent definition / config / prompt."
                rows={8}
                required
              />
            </div>
          </CardContent>
        </Card>

        {/* ── Manifest core (required) ── */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Manifest core</CardTitle>
            <CardDescription>
              Entry point and soul/prompt reference (ADR §3.3).
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <label htmlFor="schema-version">Schema version *</label>
                <Input
                  id="schema-version"
                  value={schemaVersion}
                  onChange={(e) => setSchemaVersion(e.target.value)}
                />
              </div>
              <div className="space-y-2">
                <label htmlFor="agent-entry">Agent entry *</label>
                <Input
                  id="agent-entry"
                  value={agentEntry}
                  onChange={(e) => setAgentEntry(e.target.value)}
                  placeholder="e.g. soul"
                  required
                />
              </div>
            </div>
            <div className="space-y-2">
              <label htmlFor="soul-prompt">Soul / prompt ref</label>
              <Textarea
                id="soul-prompt"
                value={soulOrPromptRef}
                onChange={(e) => setSoulOrPromptRef(e.target.value)}
                placeholder="Stable reference to the agent's soul/prompt (never plaintext secrets)."
                rows={3}
              />
            </div>
          </CardContent>
        </Card>

        {/* ── Model requirements ── */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Model requirements</CardTitle>
          </CardHeader>
          <CardContent>
            <DynamicListRow<ModelRequirement>
              fields={[{ key: "name", label: "model name", required: true }]}
              value={modelRequirements}
              onChange={setModelRequirements}
              addLabel="Add model"
              createRow={() => ({ name: "" })}
            />
          </CardContent>
        </Card>

        {/* ── Skills ── */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Skills</CardTitle>
            <CardDescription>
              Stable name + optional version/digest (ADR §3.3).
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
              addLabel="Add skill"
              createRow={() => ({ name: "" })}
            />
          </CardContent>
        </Card>

        {/* ── Tools ── */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Tools</CardTitle>
          </CardHeader>
          <CardContent>
            <DynamicListRow<string>
              fields={[]}
              value={tools}
              onChange={setTools}
              addLabel="Add tool"
              createRow={() => ""}
            />
          </CardContent>
        </Card>

        {/* ── MCP servers ── */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">MCP servers</CardTitle>
            <CardDescription>
              Stable id + optional version (ADR §3.3).
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
              addLabel="Add MCP server"
              createRow={() => ({ name: "" })}
            />
          </CardContent>
        </Card>

        {/* ── Dependencies ── */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Dependencies</CardTitle>
            <CardDescription>
              Explicit dependency locks (ADR §3.3).
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
              addLabel="Add dependency"
              createRow={() => ({ name: "" })}
            />
          </CardContent>
        </Card>

        {/* ── Network requirements ── */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Network requirements</CardTitle>
            <CardDescription>
              Explicit network egress declarations (ADR §3.3).
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
              addLabel="Add network requirement"
              createRow={() => ({ host: "" })}
            />
          </CardContent>
        </Card>

        {/* ── Secret requirements ── */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Secret requirements</CardTitle>
            <CardDescription>
              Secret references — name + ref only, never plaintext (ADR §3.3).
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
              addLabel="Add secret ref"
              createRow={() => ({ name: "", ref: "" })}
            />
          </CardContent>
        </Card>

        {/* ── Runtime limits ── */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Runtime limits</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-3 gap-4">
              <div className="space-y-2">
                <label htmlFor="max-steps">Max steps</label>
                <Input
                  id="max-steps"
                  type="number"
                  value={maxSteps}
                  onChange={(e) => setMaxSteps(e.target.value)}
                  placeholder="—"
                />
              </div>
              <div className="space-y-2">
                <label htmlFor="max-tokens">Max tokens</label>
                <Input
                  id="max-tokens"
                  type="number"
                  value={maxTokens}
                  onChange={(e) => setMaxTokens(e.target.value)}
                  placeholder="—"
                />
              </div>
              <div className="space-y-2">
                <label htmlFor="timeout">Timeout (s)</label>
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
            Cancel
          </Button>
          <Button type="submit" disabled={!canSubmit}>
            {createVersion.isPending ? "Creating…" : "Create version"}
          </Button>
        </div>
      </form>
    </div>
  );
}

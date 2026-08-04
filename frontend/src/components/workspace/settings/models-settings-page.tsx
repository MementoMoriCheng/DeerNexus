"use client";

import {
  BoxesIcon,
  KeyRoundIcon,
  PencilIcon,
  PlusIcon,
  Trash2Icon,
} from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { useI18n } from "@/core/i18n/hooks";
import {
  useCreateModelProvider,
  useDeleteModelProvider,
  useModelProviders,
  useUpdateModelProvider,
} from "@/core/model-providers/hooks";
import type { ModelProvider } from "@/core/model-providers/types";

import { SettingsSection } from "./settings-section";

const NAME_PATTERN = /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/;

type FormState = {
  name: string;
  display_name: string;
  description: string;
  model: string;
  base_url: string;
  api_key: string;
  use: string;
  supports_thinking: boolean;
  supports_reasoning_effort: boolean;
};

const EMPTY_FORM: FormState = {
  name: "",
  display_name: "",
  description: "",
  model: "",
  base_url: "",
  api_key: "",
  use: "langchain_openai:ChatOpenAI",
  supports_thinking: false,
  supports_reasoning_effort: false,
};

export function ModelsSettingsPage() {
  const { t } = useI18n();
  const { providers, isLoading } = useModelProviders();
  const createMutation = useCreateModelProvider();
  const updateMutation = useUpdateModelProvider();
  const deleteMutation = useDeleteModelProvider();

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<ModelProvider | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [errors, setErrors] = useState<
    Partial<Record<keyof FormState, string>>
  >({});

  useEffect(() => {
    if (!dialogOpen) {
      setEditing(null);
      setForm(EMPTY_FORM);
      setErrors({});
    }
  }, [dialogOpen]);

  function openCreate() {
    setEditing(null);
    setForm(EMPTY_FORM);
    setErrors({});
    setDialogOpen(true);
  }

  function openEdit(provider: ModelProvider) {
    setEditing(provider);
    setForm({
      name: provider.name,
      display_name: provider.display_name ?? "",
      description: provider.description ?? "",
      model: provider.model,
      base_url: provider.base_url ?? "",
      api_key: "",
      use: provider.use,
      supports_thinking: provider.supports_thinking,
      supports_reasoning_effort: provider.supports_reasoning_effort,
    });
    setErrors({});
    setDialogOpen(true);
  }

  function validate(): boolean {
    const next: Partial<Record<keyof FormState, string>> = {};
    if (!form.name.trim()) {
      next.name = t.settings.models.validation.nameRequired;
    } else if (!NAME_PATTERN.test(form.name.trim())) {
      next.name = t.settings.models.validation.nameInvalid;
    }
    if (!form.model.trim()) {
      next.model = t.settings.models.validation.modelRequired;
    }
    if (!form.base_url.trim()) {
      next.base_url = t.settings.models.validation.baseUrlRequired;
    }
    if (!editing && !form.api_key.trim()) {
      next.api_key = t.settings.models.validation.apiKeyRequired;
    }
    setErrors(next);
    return Object.keys(next).length === 0;
  }

  async function handleSubmit() {
    if (!validate()) return;
    const isEditing = editing !== null;
    try {
      if (isEditing && editing) {
        await updateMutation.mutateAsync({
          providerId: editing.id,
          request: {
            display_name: form.display_name.trim() || null,
            description: form.description.trim() || null,
            model: form.model.trim(),
            base_url: form.base_url.trim() || null,
            use: form.use,
            supports_thinking: form.supports_thinking,
            supports_reasoning_effort: form.supports_reasoning_effort,
            ...(form.api_key.trim() ? { api_key: form.api_key.trim() } : {}),
          },
        });
        toast.success(t.settings.models.updateSuccess);
      } else {
        await createMutation.mutateAsync({
          name: form.name.trim(),
          model: form.model.trim(),
          api_key: form.api_key.trim(),
          base_url: form.base_url.trim() || null,
          display_name: form.display_name.trim() || null,
          description: form.description.trim() || null,
          use: form.use,
          supports_thinking: form.supports_thinking,
          supports_reasoning_effort: form.supports_reasoning_effort,
        });
        toast.success(t.settings.models.createSuccess);
      }
      setDialogOpen(false);
    } catch {
      // onError toast already surfaced by the hook.
    }
  }

  async function handleDelete(provider: ModelProvider) {
    const ok = window.confirm(t.settings.models.deleteConfirm);
    if (!ok) return;
    try {
      await deleteMutation.mutateAsync(provider.id);
      toast.success(t.settings.models.deleteSuccess);
    } catch {
      // onError toast already surfaced by the hook.
    }
  }

  const submitting = createMutation.isPending || updateMutation.isPending;

  return (
    <SettingsSection
      title={t.settings.models.title}
      description={t.settings.models.description}
    >
      <div className="flex items-center justify-between gap-2">
        <p className="text-muted-foreground text-xs">
          {t.settings.models.securityHint}
        </p>
        <Button size="sm" onClick={openCreate}>
          <PlusIcon className="size-4" />
          {t.settings.models.addButton}
        </Button>
      </div>

      <div className="mt-4 space-y-3">
        {isLoading ? (
          <div className="text-muted-foreground text-sm">…</div>
        ) : providers.length === 0 ? (
          <div className="rounded-lg border border-dashed p-8 text-center">
            <BoxesIcon className="text-muted-foreground mx-auto size-8" />
            <div className="mt-2 font-medium">
              {t.settings.models.emptyTitle}
            </div>
            <div className="text-muted-foreground mt-1 text-sm">
              {t.settings.models.emptyDescription}
            </div>
          </div>
        ) : (
          providers.map((provider) => (
            <div
              key={provider.id}
              className="flex items-start justify-between gap-3 rounded-lg border p-4"
            >
              <div className="min-w-0 space-y-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{provider.name}</span>
                  {provider.display_name && (
                    <span className="text-muted-foreground text-sm">
                      {provider.display_name}
                    </span>
                  )}
                  {provider.has_api_key ? (
                    <Badge variant="secondary" className="gap-1">
                      <KeyRoundIcon className="size-3" />
                      {t.settings.models.apiKeySet}
                    </Badge>
                  ) : (
                    <Badge variant="outline">
                      {t.settings.models.apiKeyUnset}
                    </Badge>
                  )}
                </div>
                <div className="text-muted-foreground truncate text-sm">
                  {provider.model}
                  {provider.base_url ? ` · ${provider.base_url}` : ""}
                </div>
              </div>
              <div className="flex shrink-0 gap-1">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => openEdit(provider)}
                >
                  <PencilIcon className="size-4" />
                  {t.settings.models.editButton}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => handleDelete(provider)}
                  disabled={deleteMutation.isPending}
                >
                  <Trash2Icon className="size-4" />
                  {t.settings.models.deleteButton}
                </Button>
              </div>
            </div>
          ))
        )}
      </div>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>
              {editing
                ? t.settings.models.dialog.editTitle
                : t.settings.models.dialog.createTitle}
            </DialogTitle>
            <DialogDescription>
              {t.settings.models.dialog.description}
            </DialogDescription>
          </DialogHeader>

          <div className="max-h-[60vh] space-y-4 overflow-y-auto py-2">
            <FormField
              label={t.settings.models.dialog.nameLabel}
              hint={t.settings.models.dialog.nameHint}
              error={errors.name}
            >
              <Input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder={t.settings.models.dialog.namePlaceholder}
                disabled={editing !== null}
                aria-invalid={errors.name ? true : undefined}
              />
            </FormField>

            <FormField label={t.settings.models.dialog.displayNameLabel}>
              <Input
                value={form.display_name}
                onChange={(e) =>
                  setForm({ ...form, display_name: e.target.value })
                }
                placeholder={t.settings.models.dialog.displayNamePlaceholder}
              />
            </FormField>

            <FormField
              label={t.settings.models.dialog.modelLabel}
              error={errors.model}
            >
              <Input
                value={form.model}
                onChange={(e) => setForm({ ...form, model: e.target.value })}
                placeholder={t.settings.models.dialog.modelPlaceholder}
                aria-invalid={errors.model ? true : undefined}
              />
            </FormField>

            <FormField
              label={t.settings.models.dialog.baseUrlLabel}
              error={errors.base_url}
            >
              <Input
                value={form.base_url}
                onChange={(e) => setForm({ ...form, base_url: e.target.value })}
                placeholder={t.settings.models.dialog.baseUrlPlaceholder}
                aria-invalid={errors.base_url ? true : undefined}
              />
            </FormField>

            <FormField
              label={t.settings.models.dialog.apiKeyLabel}
              error={errors.api_key}
              hint={
                editing ? t.settings.models.dialog.apiKeyEditHint : undefined
              }
            >
              <Input
                type="password"
                value={form.api_key}
                onChange={(e) => setForm({ ...form, api_key: e.target.value })}
                placeholder={t.settings.models.dialog.apiKeyPlaceholder}
                aria-invalid={errors.api_key ? true : undefined}
              />
            </FormField>

            <FormField label={t.settings.models.dialog.descriptionLabel}>
              <Textarea
                value={form.description}
                onChange={(e) =>
                  setForm({ ...form, description: e.target.value })
                }
                placeholder={t.settings.models.dialog.descriptionPlaceholder}
                rows={2}
              />
            </FormField>

            <div className="space-y-3">
              <ToggleRow
                label={t.settings.models.dialog.thinkingLabel}
                checked={form.supports_thinking}
                onCheckedChange={(v) =>
                  setForm({ ...form, supports_thinking: v })
                }
              />
              <ToggleRow
                label={t.settings.models.dialog.reasoningEffortLabel}
                checked={form.supports_reasoning_effort}
                onCheckedChange={(v) =>
                  setForm({ ...form, supports_reasoning_effort: v })
                }
              />
            </div>
          </div>

          <DialogFooter>
            <DialogClose asChild>
              <Button variant="outline">
                {t.settings.models.dialog.cancelButton}
              </Button>
            </DialogClose>
            <Button onClick={handleSubmit} disabled={submitting}>
              {editing
                ? t.settings.models.dialog.submitButtonEditing
                : t.settings.models.dialog.submitButton}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </SettingsSection>
  );
}

function FormField({
  label,
  hint,
  error,
  children,
}: {
  label: string;
  hint?: string;
  error?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <label className="text-sm font-medium">{label}</label>
      {children}
      {hint && !error && (
        <p className="text-muted-foreground text-xs">{hint}</p>
      )}
      {error && <p className="text-destructive text-xs">{error}</p>}
    </div>
  );
}

function ToggleRow({
  label,
  checked,
  onCheckedChange,
}: {
  label: string;
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
}) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-sm font-medium">{label}</span>
      <Switch checked={checked} onCheckedChange={onCheckedChange} />
    </div>
  );
}

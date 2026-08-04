/**
 * Model-provider REST client — talks to the per-user custom model-provider
 * CRUD API (PR-A, `/api/model-providers*`).
 *
 * Mirrors the `core/channels/api.ts` pattern: CSRF-wrapped `fetch` from
 * `@/core/api/fetcher` (credentials + X-CSRF-Token + 401→/login redirect are
 * free), and a `throwModelProviderApiError` helper that surfaces the FastAPI
 * `detail` string so the UI can show "name already in use" (409) etc.
 *
 * The cleartext API key is sent ONLY on create / rotate-update; the list/get
 * responses never include it (the server exposes `has_api_key` instead).
 */

import { fetch } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";

import type {
  CreateModelProviderRequest,
  ModelProvider,
  UpdateModelProviderRequest,
} from "./types";

function apiUrl(path: string): string {
  return `${getBackendBaseURL()}/api/model-providers${path}`;
}

async function throwModelProviderApiError(
  response: Response,
  fallback: string,
): Promise<never> {
  const body = (await response.json().catch(() => ({}))) as {
    detail?: unknown;
  };
  throw new Error(typeof body.detail === "string" ? body.detail : fallback);
}

export async function listModelProviders(): Promise<ModelProvider[]> {
  const response = await fetch(apiUrl(""));
  if (!response.ok) {
    await throwModelProviderApiError(
      response,
      `Failed to load model providers: ${response.statusText}`,
    );
  }
  return (await response.json()) as ModelProvider[];
}

export async function createModelProvider(
  request: CreateModelProviderRequest,
): Promise<ModelProvider> {
  const response = await fetch(apiUrl(""), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  if (!response.ok) {
    await throwModelProviderApiError(
      response,
      `Failed to create model provider: ${response.statusText}`,
    );
  }
  return (await response.json()) as ModelProvider;
}

export async function updateModelProvider(
  providerId: string,
  request: UpdateModelProviderRequest,
): Promise<ModelProvider> {
  const response = await fetch(apiUrl(`/${encodeURIComponent(providerId)}`), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  if (!response.ok) {
    await throwModelProviderApiError(
      response,
      `Failed to update model provider: ${response.statusText}`,
    );
  }
  return (await response.json()) as ModelProvider;
}

export async function deleteModelProvider(providerId: string): Promise<void> {
  const response = await fetch(apiUrl(`/${encodeURIComponent(providerId)}`), {
    method: "DELETE",
  });
  if (!response.ok) {
    await throwModelProviderApiError(
      response,
      `Failed to delete model provider: ${response.statusText}`,
    );
  }
}

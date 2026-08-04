/**
 * Model-provider domain types — plain TS interfaces mirroring the backend
 * response envelopes for the per-user custom model-provider CRUD API (PR-A,
 * `/api/model-providers*`).
 *
 * Field names match the Pydantic `ModelProviderResponse` / request models 1:1
 * so a backend rename surfaces as a type error here. The cleartext API key is
 * NEVER transported: the response exposes only `has_api_key`; create/update
 * requests send the key once and the server stores it encrypted.
 *
 * Source of truth: backend/app/gateway/routers/model_providers.py
 */

/** 1:1 projection of ModelProviderResponse. The API key is never echoed. */
export interface ModelProvider {
  id: string;
  name: string;
  display_name: string | null;
  description: string | null;
  model: string;
  use: string;
  base_url: string | null;
  supports_thinking: boolean;
  supports_reasoning_effort: boolean;
  /** True when the server holds an encrypted key for this provider. */
  has_api_key: boolean;
  created_at: string; // ISO 8601
  updated_at: string;
}

/** Body of POST /api/model-providers (ModelProviderCreateRequest). */
export interface CreateModelProviderRequest {
  name: string;
  model: string;
  api_key: string;
  display_name?: string | null;
  description?: string | null;
  base_url?: string | null;
  use?: string;
  supports_thinking?: boolean;
  supports_reasoning_effort?: boolean;
}

/** Body of PUT /api/model-providers/{id} (ModelProviderUpdateRequest). */
export interface UpdateModelProviderRequest {
  display_name?: string | null;
  description?: string | null;
  model?: string;
  base_url?: string | null;
  use?: string;
  /**
   * Omit to keep the existing key; send a new value to rotate. The UI leaves
   * this empty on edit unless the user types a new key.
   */
  api_key?: string;
  supports_thinking?: boolean;
  supports_reasoning_effort?: boolean;
}

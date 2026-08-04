/**
 * TanStack Query hooks for the per-user custom model-provider CRUD API.
 *
 * Reads use `useQuery`. Writes (create/update/delete) use `useMutation` that
 * invalidates the `["modelProviders"]` list on success so the UI re-fetches a
 * strong-consistent view after each write. Errors surface a sonner toast with
 * the backend `detail` (e.g. "Provider name already in use" on 409).
 *
 * All hooks inherit the shared `QueryClient` defaults
 * (`refetchOnWindowFocus: false`).
 */

"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  createModelProvider,
  deleteModelProvider,
  listModelProviders,
  updateModelProvider,
} from "./api";
import type {
  CreateModelProviderRequest,
  UpdateModelProviderRequest,
} from "./types";

export const modelProvidersQueryKey = ["modelProviders"] as const;

export function useModelProviders() {
  const { data, isLoading, error } = useQuery({
    queryKey: modelProvidersQueryKey,
    queryFn: () => listModelProviders(),
  });
  return { providers: data ?? [], isLoading, error };
}

export function useCreateModelProvider() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: CreateModelProviderRequest) =>
      createModelProvider(request),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: modelProvidersQueryKey });
    },
    onError: (error) => {
      toast.error(error instanceof Error ? error.message : "Failed to create");
    },
  });
}

export function useUpdateModelProvider() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      providerId,
      request,
    }: {
      providerId: string;
      request: UpdateModelProviderRequest;
    }) => updateModelProvider(providerId, request),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: modelProvidersQueryKey });
    },
    onError: (error) => {
      toast.error(error instanceof Error ? error.message : "Failed to update");
    },
  });
}

export function useDeleteModelProvider() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (providerId: string) => deleteModelProvider(providerId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: modelProvidersQueryKey });
    },
    onError: (error) => {
      toast.error(error instanceof Error ? error.message : "Failed to delete");
    },
  });
}

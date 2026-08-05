import { beforeEach, describe, expect, test, rs } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({
  fetch: rs.fn(),
}));

rs.mock("@/core/config", () => ({
  getBackendBaseURL: () => "/backend",
}));

import { fetch as fetcher } from "@/core/api/fetcher";
import {
  createModelProvider,
  deleteModelProvider,
  listModelProviders,
  updateModelProvider,
} from "@/core/model-providers/api";

const mockedFetch = rs.mocked(fetcher);

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    statusText: status >= 400 ? "Bad Request" : "OK",
    headers: { "Content-Type": "application/json" },
  });
}

const SAMPLE_PROVIDER = {
  id: "p1",
  name: "my-deepseek",
  display_name: "My DeepSeek",
  description: null,
  model: "deepseek-chat",
  use: "langchain_openai:ChatOpenAI",
  base_url: "https://api.deepseek.com/v1",
  supports_thinking: false,
  supports_reasoning_effort: false,
  has_api_key: true,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

beforeEach(() => {
  mockedFetch.mockReset();
});

describe("model-providers api", () => {
  test("listModelProviders GETs the collection and unwraps { providers: [...] }", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, { providers: [SAMPLE_PROVIDER] }),
    );

    const result = await listModelProviders();

    expect(result).toEqual([SAMPLE_PROVIDER]);
    expect(mockedFetch).toHaveBeenCalledWith("/backend/api/model-providers");
  });

  test("listModelProviders returns empty list when backend responds { providers: [] }", async () => {
    mockedFetch.mockResolvedValueOnce(jsonResponse(200, { providers: [] }));

    await expect(listModelProviders()).resolves.toEqual([]);
  });

  test("listModelProviders tolerates a missing providers field", async () => {
    mockedFetch.mockResolvedValueOnce(jsonResponse(200, {}));

    await expect(listModelProviders()).resolves.toEqual([]);
  });

  test("createModelProvider POSTs JSON with the api_key", async () => {
    mockedFetch.mockResolvedValueOnce(jsonResponse(201, SAMPLE_PROVIDER));

    const result = await createModelProvider({
      name: "my-deepseek",
      model: "deepseek-chat",
      api_key: "sk-secret",
      base_url: "https://api.deepseek.com/v1",
    });

    expect(result).toEqual(SAMPLE_PROVIDER);
    expect(mockedFetch).toHaveBeenCalledWith(
      "/backend/api/model-providers",
      expect.objectContaining({
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: "my-deepseek",
          model: "deepseek-chat",
          api_key: "sk-secret",
          base_url: "https://api.deepseek.com/v1",
        }),
      }),
    );
  });

  test("updateModelProvider PUTs to /{id} and omits api_key when blank", async () => {
    mockedFetch.mockResolvedValueOnce(jsonResponse(200, SAMPLE_PROVIDER));

    await updateModelProvider("p1", {
      display_name: "Renamed",
      model: "deepseek-v2",
    });

    expect(mockedFetch).toHaveBeenCalledWith(
      "/backend/api/model-providers/p1",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({
          display_name: "Renamed",
          model: "deepseek-v2",
        }),
      }),
    );
  });

  test("deleteModelProvider DELETEs /{id}", async () => {
    // 204 No Content has no body.
    mockedFetch.mockResolvedValueOnce(new Response(null, { status: 204 }));

    await deleteModelProvider("p1");

    expect(mockedFetch).toHaveBeenCalledWith(
      "/backend/api/model-providers/p1",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  test("createModelProvider throws with backend detail on 409 name clash", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(409, { detail: "Provider name already in use" }),
    );

    await expect(
      createModelProvider({
        name: "dup",
        model: "m",
        api_key: "k",
      }),
    ).rejects.toThrow("Provider name already in use");
  });

  test("listModelProviders throws on non-ok with status fallback", async () => {
    mockedFetch.mockResolvedValueOnce(jsonResponse(500, { unexpected: true }));

    await expect(listModelProviders()).rejects.toThrow(
      /Failed to load model providers/,
    );
  });

  test("updateModelProvider encodes the provider id in the URL path", async () => {
    mockedFetch.mockResolvedValueOnce(jsonResponse(200, SAMPLE_PROVIDER));

    await updateModelProvider("weird/id", { model: "m" });

    expect(mockedFetch).toHaveBeenCalledWith(
      "/backend/api/model-providers/weird%2Fid",
      expect.objectContaining({ method: "PUT" }),
    );
  });
});

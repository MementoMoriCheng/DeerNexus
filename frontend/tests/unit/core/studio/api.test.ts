/**
 * Tests for the Studio REST client (`core/studio/api.ts`).
 *
 * Pins three contracts:
 *   1. GET fetchers hit the correct URL and return the parsed body.
 *   2. Non-2xx responses throw `StudioRequestError` carrying the parsed
 *      ContractError envelope `{code, message, retryable}` (so the UI can
 *      surface the exact release error code + retryability).
 *   3. promote/rollback send the CAS `If-Match` header (quoted row_version)
 *      and `Idempotency-Key`, and omit the body `expected_channel_version`
 *      when If-Match is present (exactly-one validation, PR-055).
 *
 * Mirrors the `core/agents/api.test.ts` mock-fetch pattern.
 */
import { beforeEach, describe, expect, test, rs } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({
  fetch: rs.fn(),
}));

rs.mock("@/core/config", () => ({
  getBackendBaseURL: () => "",
}));

import {
  listPackages,
  promoteChannel,
  publishVersion,
  StudioRequestError,
} from "@/core/studio/api";
import { fetch as fetcher } from "@/core/api/fetcher";

const mockedFetch = rs.mocked(fetcher);

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

beforeEach(() => {
  mockedFetch.mockReset();
});

describe("listPackages", () => {
  test("returns parsed array on 200", async () => {
    const packages = [{ id: "pkg-1", name: "alpha", display_name: "Alpha" }];
    mockedFetch.mockResolvedValueOnce(jsonResponse(200, packages));
    const result = await listPackages();
    expect(result).toEqual(packages);
    expect(mockedFetch.mock.calls[0][0]).toBe("/api/v1/agent-packages");
  });

  test("throws StudioRequestError on 403 with parsed ContractError envelope", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(403, {
        detail: { code: "permission_denied", message: "Permission denied", retryable: false },
      }),
    );
    await expect(listPackages()).rejects.toMatchObject({
      name: "StudioRequestError",
      status: 403,
      code: "permission_denied",
      message: "Permission denied",
      retryable: false,
    });
  });

  test("403 exposes isPermissionDenied getter", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(403, { detail: "no perm" }),
    );
    try {
      await listPackages();
      expect.fail("should have thrown");
    } catch (e) {
      expect(e).toBeInstanceOf(StudioRequestError);
      expect((e as StudioRequestError).isPermissionDenied).toBe(true);
    }
  });

  test("parses string-detail legacy error (no code/retryable)", async () => {
    mockedFetch.mockResolvedValueOnce(jsonResponse(500, { detail: "boom" }));
    await expect(listPackages()).rejects.toMatchObject({
      status: 500,
      code: null,
      retryable: false,
      message: "boom",
    });
  });
});

describe("publishVersion", () => {
  test("POSTs to :publish and returns the version", async () => {
    const version = { id: "v-1", version: "1.0.0", status: "published" };
    mockedFetch.mockResolvedValueOnce(jsonResponse(200, version));
    const result = await publishVersion("v-1");
    expect(result).toEqual(version);
    const call = mockedFetch.mock.calls[0];
    expect(call[0]).toBe("/api/v1/agent-versions/v-1:publish");
    expect((call[1] as RequestInit).method).toBe("POST");
  });

  test("throws StudioRequestError on non-2xx", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(409, {
        detail: { code: "release_conflict", message: "CAS mismatch", retryable: true },
      }),
    );
    await expect(publishVersion("v-1")).rejects.toMatchObject({
      status: 409,
      code: "release_conflict",
      retryable: true,
    });
  });
});

describe("promoteChannel (CAS + Idempotency-Key)", () => {
  test("sends If-Match header (quoted row_version) and omits body expected_channel_version", async () => {
    const response = { channel: { id: "ch-1" }, event: { id: "e-1" } };
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, response, { ETag: '"2"' }),
    );
    await promoteChannel(
      "pkg-1",
      "prod",
      { target_version_id: "v-2", expected_channel_version: 1 },
      { ifMatch: 1, idempotencyKey: "idem-abc" },
    );
    const call = mockedFetch.mock.calls[0];
    const init = call[1] as RequestInit;
    const headers = init.headers as Record<string, string>;
    expect(headers["If-Match"]).toBe('"1"');
    expect(headers["Idempotency-Key"]).toBe("idem-abc");
    // Body omits expected_channel_version because If-Match is present.
    const body = JSON.parse(init.body as string);
    expect(body).not.toHaveProperty("expected_channel_version");
    expect(body.target_version_id).toBe("v-2");
    expect(call[0]).toBe("/api/v1/agent-packages/pkg-1/channels/prod:promote");
  });

  test("falls back to body expected_channel_version when no If-Match header", async () => {
    mockedFetch.mockResolvedValueOnce(jsonResponse(200, { channel: {}, event: {} }));
    await promoteChannel("pkg-1", "dev", {
      target_version_id: "v-1",
      expected_channel_version: 3,
    });
    const init = mockedFetch.mock.calls[0][1] as RequestInit;
    const headers = init.headers as Record<string, string>;
    expect(headers["If-Match"]).toBe('"3"');
    const body = JSON.parse(init.body as string);
    expect(body).not.toHaveProperty("expected_channel_version");
  });

  test("throws StudioRequestError on 409 release_gate_violation (non-retryable)", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(409, {
        detail: {
          code: "release_gate_violation",
          message: "prod requires published",
          retryable: false,
        },
      }),
    );
    await expect(
      promoteChannel("pkg-1", "prod", { target_version_id: "v-1", expected_channel_version: 1 }),
    ).rejects.toMatchObject({
      status: 409,
      code: "release_gate_violation",
      retryable: false,
    });
  });
});

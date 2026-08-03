import { beforeEach, describe, expect, it, vi } from "vitest";
import { API_ROOT, ApiError, api, artifactContentUrl } from "./client";

function mockResponse(payload: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: vi.fn().mockResolvedValue(payload),
    text: vi.fn().mockResolvedValue(
      typeof payload === "string" ? payload : JSON.stringify(payload),
    ),
  } as unknown as Response;
}

describe("API client", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  it("uses the nginx /encoder API prefix and preserves repeated state filters", async () => {
    fetchMock.mockResolvedValue(
      mockResponse({ items: [], meta: { limit: 25, offset: 10, count: 0 } }),
    );

    await api.jobs(["QUEUED", "FAILED"], 25, 10);

    expect(API_ROOT).toBe("/encoder/api/v1");
    expect(fetchMock).toHaveBeenCalledWith(
      "/encoder/api/v1/jobs?limit=25&offset=10&state=QUEUED&state=FAILED",
      expect.objectContaining({
        credentials: "same-origin",
        headers: expect.objectContaining({ Accept: "application/json" }),
      }),
    );
    expect(artifactContentUrl("frame/1")).toBe(
      "/encoder/api/v1/artifacts/frame%2F1/content",
    );
  });

  it("turns backend error details into a typed ApiError", async () => {
    fetchMock.mockResolvedValue(
      mockResponse({ detail: "Már fut egy aktív kódolás", current_state: "ENCODING" }, 409),
    );

    const request = api.createJob({
      source_path: "/storage/Movie",
      disc_type: "BD",
      content_type: "FILM",
      priority: 0,
      settings: {},
    });

    const error: unknown = await request.catch((value: unknown) => value);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toEqual(
      expect.objectContaining({
        name: "ApiError",
        status: 409,
        detail: "Már fut egy aktív kódolás",
        payload: expect.objectContaining({ current_state: "ENCODING" }),
      }),
    );
  });

  it("retries a failed job through the encoded retry endpoint", async () => {
    fetchMock.mockResolvedValue(mockResponse({ id: "job/1", state: "MUXING" }));

    await api.retryJob("job/1", 7);

    expect(fetchMock).toHaveBeenCalledWith(
      "/encoder/api/v1/jobs/job%2F1/retry",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ expected_version: 7 }),
      }),
    );
  });

  it("preserves a plain-text proxy error after reading the response body once", async () => {
    fetchMock.mockResolvedValue(
      new Response("A backend átmenetileg nem érhető el", {
        status: 502,
        headers: { "Content-Type": "text/plain; charset=utf-8" },
      }),
    );

    const error: unknown = await api.health().catch((value: unknown) => value);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toEqual(
      expect.objectContaining({
        status: 502,
        detail: "A backend átmenetileg nem érhető el",
      }),
    );
  });
});

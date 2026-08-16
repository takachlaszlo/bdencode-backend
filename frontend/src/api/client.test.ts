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

  it("restarts and permanently deletes a cancelled job with version guards", async () => {
    fetchMock
      .mockResolvedValueOnce(mockResponse({ id: "job/1", state: "READY" }))
      .mockResolvedValueOnce(mockResponse(null, 204));

    await api.restartJob("job/1", 8);
    await api.purgeJob("job/1", 9);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/encoder/api/v1/jobs/job%2F1/restart",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ expected_version: 8 }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/encoder/api/v1/jobs/job%2F1/purge?expected_version=9&preserve_release=true",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("sends durable pause, continue and cancel requests with control revisions", async () => {
    fetchMock.mockResolvedValue(mockResponse({ id: "job/1", control_state: "PAUSED" }));

    await api.pauseJob("job/1", 3);
    await api.continueJob("job/1", 4);
    await api.requestCancelJob("job/1", 5);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/encoder/api/v1/jobs/job%2F1/pause",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ expected_control_revision: 3 }) }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/encoder/api/v1/jobs/job%2F1/continue",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ expected_control_revision: 4 }) }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "/encoder/api/v1/jobs/job%2F1/cancel",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ expected_control_revision: 5 }) }),
    );
  });

  it("uses guarded maintenance and release-preparation endpoints", async () => {
    fetchMock
      .mockResolvedValueOnce(mockResponse({ workspace_bytes: 10, reclaimable_bytes: 5, completed_release_bytes: 20, categories: [] }))
      .mockResolvedValueOnce(mockResponse({ operation_id: "cleanup-1" }))
      .mockResolvedValueOnce(mockResponse({ id: "prep/1", state: "READY", version: 7 }))
      .mockResolvedValueOnce(mockResponse({ valid: true, failures: [], payload: {}, screenshots: 6 }))
      .mockResolvedValueOnce(mockResponse({ id: "prep/1", state: "PUBLISHED", version: 8 }))
      .mockResolvedValueOnce(mockResponse(null, 204))
      .mockResolvedValueOnce(mockResponse(null, 204));

    const manifestSha256 = "b".repeat(64);
    await api.jobStorage("job/1");
    await api.cleanupJob("job/1", 6);
    await api.releasePreparationAction("prep/1", "dupe-check", 7);
    await api.validateReleasePreparation("prep/1", 7);
    await api.uploadReleasePreparation("prep/1", { expected_version: 7, manifest_sha256: manifestSha256 });
    await api.deleteReleasePreparation("prep/1", 8);
    await api.deleteJobRelease("job/1", { confirmation: "Release.Name", expected_sha256: "a".repeat(64), force_if_seeded: false, preparation_versions: { "prep/1": 8 } });

    expect(fetchMock).toHaveBeenNthCalledWith(1, "/encoder/api/v1/jobs/job%2F1/storage", expect.anything());
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/encoder/api/v1/jobs/job%2F1/cleanup", expect.objectContaining({ method: "POST", body: JSON.stringify({ scope: "temporary", expected_version: 6 }) }));
    expect(fetchMock).toHaveBeenNthCalledWith(3, "/encoder/api/v1/release-preparations/prep%2F1/dupe-check", expect.objectContaining({ method: "POST", body: JSON.stringify({ expected_version: 7 }) }));
    expect(fetchMock).toHaveBeenNthCalledWith(4, "/encoder/api/v1/release-preparations/prep%2F1/validate", expect.objectContaining({ method: "POST", body: JSON.stringify({ expected_version: 7 }) }));
    expect(fetchMock).toHaveBeenNthCalledWith(5, "/encoder/api/v1/release-preparations/prep%2F1/upload", expect.objectContaining({
      method: "POST",
      headers: expect.objectContaining({ "X-BDEncode-Manifest": manifestSha256 }),
      body: JSON.stringify({ expected_version: 7, manifest_sha256: manifestSha256 }),
    }));
    expect(fetchMock).toHaveBeenNthCalledWith(6, "/encoder/api/v1/release-preparations/prep%2F1?expected_version=8", expect.objectContaining({ method: "DELETE" }));
    expect(fetchMock).toHaveBeenNthCalledWith(7, "/encoder/api/v1/jobs/job%2F1/release", expect.objectContaining({ method: "DELETE", body: JSON.stringify({ confirmation: "Release.Name", expected_sha256: "a".repeat(64), force_if_seeded: false, preparation_versions: { "prep/1": 8 } }) }));
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

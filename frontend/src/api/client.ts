import type {
  AIRecommendationRequest,
  AIRecommendationResponse,
  AIRecommendationStatus,
  ArtifactList,
  CapabilitiesResponse,
  DetailLevel,
  EventList,
  HealthResponse,
  Job,
  JobCreate,
  JobList,
  JobStorageReport,
  JobState,
  ProfileRecommendationResponse,
  ProfileSchemaResponse,
  ReleaseMetadataPayload,
  ReleasePreparation,
  ReleasePreparationList,
  ReleaseProfileList,
  ReleaseValidationResult,
  RuntimeCapabilitiesResponse,
  ScanList,
  SelectionPayload,
  SelectionValidation,
  SourceBrowserResponse,
  TrackerReleaseProfile,
} from "./types";

const base = import.meta.env.BASE_URL.endsWith("/")
  ? import.meta.env.BASE_URL.slice(0, -1)
  : import.meta.env.BASE_URL;

export const API_ROOT = `${base}/api/v1`;

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly payload: unknown;

  constructor(status: number, detail: string, payload: unknown) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.payload = payload;
  }
}

async function responseError(response: Response): Promise<ApiError> {
  const raw = await response.text();
  let payload: unknown = raw || null;
  let detail = `A kérés sikertelen (${response.status})`;
  if (raw) {
    try {
      payload = JSON.parse(raw) as unknown;
    } catch {
      detail = raw;
    }
  }
  if (payload && typeof payload === "object" && "detail" in payload) {
    const apiDetail = payload.detail;
    if (typeof apiDetail === "string") {
      detail = apiDetail;
    } else if (Array.isArray(apiDetail)) {
      const messages = apiDetail.flatMap((item) =>
        item && typeof item === "object" && "msg" in item && typeof item.msg === "string"
          ? [item.msg]
          : [],
      );
      if (messages.length) detail = messages.join("; ");
    }
  }
  return new ApiError(response.status, detail, payload);
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
    credentials: "same-origin",
  });

  if (!response.ok) throw await responseError(response);

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export interface DownloadedFile {
  blob: Blob;
  filename: string;
}

async function apiDownload(path: string, init: RequestInit): Promise<DownloadedFile> {
  const response = await fetch(`${API_ROOT}${path}`, {
    ...init,
    headers: {
      Accept: "application/x-bittorrent, application/octet-stream",
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
    },
    credentials: "same-origin",
  });
  if (!response.ok) throw await responseError(response);
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(disposition)?.[1];
  const plain = /filename="?([^";]+)"?/i.exec(disposition)?.[1];
  let filename = encoded ? decodeURIComponent(encoded) : plain ?? "release.torrent";
  filename = filename.replace(/[\\/\x00-\x1f\x7f]/g, "_").trim() || "release.torrent";
  return { blob: await response.blob(), filename };
}

export const api = {
  health: () => apiFetch<HealthResponse>("/health"),
  capabilities: () => apiFetch<CapabilitiesResponse>("/capabilities"),
  runtimeCapabilities: () =>
    apiFetch<RuntimeCapabilitiesResponse>("/runtime-capabilities"),
  sources: (path?: string) =>
    apiFetch<SourceBrowserResponse>(
      `/sources${path ? `?path=${encodeURIComponent(path)}` : ""}`,
    ),
  jobs: (states?: JobState[], limit = 100, offset = 0) => {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    states?.forEach((state) => params.append("state", state));
    return apiFetch<JobList>(`/jobs?${params}`);
  },
  job: (id: string) => apiFetch<Job>(`/jobs/${encodeURIComponent(id)}`),
  createJob: (request: JobCreate) =>
    apiFetch<Job>("/jobs", { method: "POST", body: JSON.stringify(request) }),
  cancelJob: (id: string) =>
    apiFetch<Job>(`/jobs/${encodeURIComponent(id)}`, { method: "DELETE" }),
  pauseJob: (id: string, expectedControlRevision?: number) =>
    apiFetch<Job>(`/jobs/${encodeURIComponent(id)}/pause`, {
      method: "POST",
      body: JSON.stringify(expectedControlRevision == null ? {} : { expected_control_revision: expectedControlRevision }),
    }),
  continueJob: (id: string, expectedControlRevision?: number) =>
    apiFetch<Job>(`/jobs/${encodeURIComponent(id)}/continue`, {
      method: "POST",
      body: JSON.stringify(expectedControlRevision == null ? {} : { expected_control_revision: expectedControlRevision }),
    }),
  requestCancelJob: (id: string, expectedControlRevision?: number) =>
    apiFetch<Job>(`/jobs/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
      body: JSON.stringify(expectedControlRevision == null ? {} : { expected_control_revision: expectedControlRevision }),
    }),
  retryJob: (id: string, expectedVersion: number) =>
    apiFetch<Job>(`/jobs/${encodeURIComponent(id)}/retry`, {
      method: "POST",
      body: JSON.stringify({ expected_version: expectedVersion }),
    }),
  restartJob: (id: string, expectedVersion: number) =>
    apiFetch<Job>(`/jobs/${encodeURIComponent(id)}/restart`, {
      method: "POST",
      body: JSON.stringify({ expected_version: expectedVersion }),
    }),
  purgeJob: (id: string, expectedVersion: number) =>
    apiFetch<void>(
      `/jobs/${encodeURIComponent(id)}/purge?expected_version=${expectedVersion}&preserve_release=true`,
      { method: "DELETE" },
    ),
  jobStorage: (id: string) =>
    apiFetch<JobStorageReport>(`/jobs/${encodeURIComponent(id)}/storage`),
  cleanupJob: (id: string, expectedVersion: number) =>
    apiFetch<unknown>(`/jobs/${encodeURIComponent(id)}/cleanup`, {
      method: "POST",
      body: JSON.stringify({ scope: "temporary", expected_version: expectedVersion }),
    }),
  deleteJobRelease: (
    id: string,
    request: {
      confirmation: string;
      expected_sha256: string;
      force_if_seeded: boolean;
      preparation_versions: Record<string, number>;
    },
  ) => apiFetch<void>(`/jobs/${encodeURIComponent(id)}/release`, { method: "DELETE", body: JSON.stringify(request) }),
  resumeJob: (id: string) =>
    apiFetch<Job>(`/jobs/${encodeURIComponent(id)}/resume`, { method: "POST" }),
  retryUpload: (id: string) =>
    apiFetch<Job>(`/jobs/${encodeURIComponent(id)}/retry-upload`, { method: "POST" }),
  releaseProfiles: () =>
    apiFetch<ReleaseProfileList | TrackerReleaseProfile[]>("/release-profiles"),
  releasePreparations: (jobId: string) =>
    apiFetch<ReleasePreparationList | ReleasePreparation[]>(
      `/jobs/${encodeURIComponent(jobId)}/release-preparations`,
    ),
  createReleasePreparation: (
    jobId: string,
    request: { profile_id: string; metadata: ReleaseMetadataPayload },
  ) =>
    apiFetch<ReleasePreparation>(
      `/jobs/${encodeURIComponent(jobId)}/release-preparations`,
      { method: "POST", body: JSON.stringify(request) },
    ),
  releasePreparation: (preparationId: string) =>
    apiFetch<ReleasePreparation>(
      `/release-preparations/${encodeURIComponent(preparationId)}`,
    ),
  releasePreparationAction: (
    preparationId: string,
    action: "build" | "dupe-check" | "seed",
    expectedVersion: number,
  ) =>
    apiFetch<ReleasePreparation>(
      `/release-preparations/${encodeURIComponent(preparationId)}/${action}`,
      {
        method: "POST",
        body: JSON.stringify({ expected_version: expectedVersion }),
      },
    ),
  validateReleasePreparation: (preparationId: string, expectedVersion: number) =>
    apiFetch<ReleaseValidationResult>(
      `/release-preparations/${encodeURIComponent(preparationId)}/validate`,
      {
        method: "POST",
        body: JSON.stringify({ expected_version: expectedVersion }),
      },
    ),
  exportReleasePreparation: (preparationId: string, expectedVersion: number) =>
    apiDownload(`/release-preparations/${encodeURIComponent(preparationId)}/export`, {
      method: "POST",
      body: JSON.stringify({ expected_version: expectedVersion }),
    }),
  uploadReleasePreparation: (
    preparationId: string,
    request: { expected_version: number; manifest_sha256: string },
  ) => apiFetch<ReleasePreparation>(
    `/release-preparations/${encodeURIComponent(preparationId)}/upload`,
    {
      method: "POST",
      headers: { "X-BDEncode-Manifest": request.manifest_sha256 },
      body: JSON.stringify(request),
    },
  ),
  deleteReleasePreparation: (preparationId: string, expectedVersion: number) =>
    apiFetch<void>(
      `/release-preparations/${encodeURIComponent(preparationId)}?expected_version=${expectedVersion}`,
      { method: "DELETE" },
    ),
  scans: (jobId: string) =>
    apiFetch<ScanList>(`/scans?job_id=${encodeURIComponent(jobId)}`),
  artifacts: (jobId: string) =>
    apiFetch<ArtifactList>(`/artifacts?job_id=${encodeURIComponent(jobId)}&limit=500`),
  events: (jobId: string, afterId = 0) =>
    apiFetch<EventList>(
      `/events?job_id=${encodeURIComponent(jobId)}&after_id=${afterId}&limit=1000`,
    ),
  analyzeMkv: (path: string) =>
    apiFetch<Record<string, unknown>>(`/analyze-mkv?path=${encodeURIComponent(path)}`),
  profileSchema: (encoder: "x264" | "x265", detail: DetailLevel) =>
    apiFetch<ProfileSchemaResponse>(
      `/profiles/${encoder}/schema?detail_level=${detail}`,
    ),
  profileRecommendation: (
    encoder: "x264" | "x265",
    detail: DetailLevel,
    contentType: string,
  ) =>
    apiFetch<ProfileRecommendationResponse>(
      `/profiles/${encoder}/recommendation?detail_level=${detail}&content_type=${encodeURIComponent(contentType.toLowerCase())}`,
    ),
  aiRecommendationStatus: () =>
    apiFetch<AIRecommendationStatus>("/ai-recommendation/status"),
  aiRecommendation: (id: string, request: AIRecommendationRequest) =>
    apiFetch<AIRecommendationResponse>(
      `/jobs/${encodeURIComponent(id)}/ai-recommendation`,
      { method: "POST", body: JSON.stringify(request) },
    ),
  validateSelection: (id: string, selection: SelectionPayload, version?: number) =>
    apiFetch<SelectionValidation>(
      `/jobs/${encodeURIComponent(id)}/selection/validate`,
      {
        method: "POST",
        body: JSON.stringify({ selection, expected_version: version }),
      },
    ),
  saveSelection: (id: string, selection: SelectionPayload, version?: number) =>
    apiFetch<Job>(`/jobs/${encodeURIComponent(id)}/selection`, {
      method: "POST",
      body: JSON.stringify({
        selection,
        message: "Operátori beállítások jóváhagyva a webes felületen",
        expected_version: version,
      }),
    }),
};

export function artifactContentUrl(id: string): string {
  return `${API_ROOT}/artifacts/${encodeURIComponent(id)}/content`;
}

export async function fetchArtifactText(id: string): Promise<string> {
  const response = await fetch(artifactContentUrl(id), {
    credentials: "same-origin",
  });
  if (!response.ok) throw new ApiError(response.status, "A melléklet nem olvasható", null);
  return response.text();
}

export async function fetchArtifactJson<T>(id: string): Promise<T> {
  const response = await fetch(artifactContentUrl(id), {
    credentials: "same-origin",
  });
  if (!response.ok) throw new ApiError(response.status, "A melléklet nem olvasható", null);
  return response.json() as Promise<T>;
}

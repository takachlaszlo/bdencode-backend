import type {
  ArtifactList,
  CapabilitiesResponse,
  DetailLevel,
  EventList,
  HealthResponse,
  Job,
  JobCreate,
  JobList,
  JobState,
  ProfileRecommendationResponse,
  ProfileSchemaResponse,
  ScanList,
  SelectionPayload,
  SelectionValidation,
  SourceBrowserResponse,
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

  if (!response.ok) {
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
    throw new ApiError(response.status, detail, payload);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  health: () => apiFetch<HealthResponse>("/health"),
  capabilities: () => apiFetch<CapabilitiesResponse>("/capabilities"),
  runtimeCapabilities: () =>
    apiFetch<Record<string, unknown>>("/runtime-capabilities"),
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
  resumeJob: (id: string) =>
    apiFetch<Job>(`/jobs/${encodeURIComponent(id)}/resume`, { method: "POST" }),
  retryUpload: (id: string) =>
    apiFetch<Job>(`/jobs/${encodeURIComponent(id)}/retry-upload`, { method: "POST" }),
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

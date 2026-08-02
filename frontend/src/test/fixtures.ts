import type { Artifact, DiscScanResult, Job, MediaStream } from "../api/types";

const now = "2026-08-02T17:00:00Z";

export function makeJob(overrides: Partial<Job> = {}): Job {
  return {
    id: "job-1",
    name: "Mintafilm",
    source_path: "/storage/Mintafilm",
    work_path: null,
    output_path: null,
    disc_type: "BD",
    content_type: "FILM",
    state: "QUEUED",
    priority: 0,
    settings: {},
    selection: null,
    requested_by: null,
    progress: null,
    status_message: null,
    error: null,
    resume_state: null,
    version: 1,
    created_at: now,
    updated_at: now,
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

export function makeArtifact(overrides: Partial<Artifact> = {}): Artifact {
  return {
    id: "artifact-1",
    job_id: "job-1",
    scan_id: null,
    kind: "OTHER",
    name: "artifact.bin",
    path: "/home/accofil/encode/jobs/job-1/artifact.bin",
    mime_type: "application/octet-stream",
    sha256: "a".repeat(64),
    size_bytes: 123,
    metadata: {},
    created_at: now,
    ...overrides,
  };
}

const video: MediaStream = {
  id: "video:4113",
  index: 0,
  pid: 4113,
  kind: "video",
  codec: "h264",
  codec_profile: "High",
  language: null,
  title: null,
  channels: null,
  channel_layout: null,
  sample_rate: null,
  bit_depth: 8,
  default: false,
  forced: false,
  roles: [],
  object_audio: false,
  video: {
    codec: "avc",
    width: 1920,
    height: 1080,
    frame_rate: "24000/1001",
    field_order: "progressive",
    bit_depth: 8,
    pixel_format: "yuv420p",
    color_primaries: "bt709",
    color_transfer: "bt709",
    color_matrix: "bt709",
    color_range: "tv",
    chroma_location: "left",
    hdr10: false,
    dolby_vision: false,
    hdr10_base_layer: false,
    hdr10_plus: false,
    three_d: false,
  },
};

const audio: MediaStream = {
  id: "audio:4352",
  index: 1,
  pid: 4352,
  kind: "audio",
  codec: "ac3",
  codec_profile: null,
  language: {
    iso639_2t: "eng",
    bcp47: "en",
    confidence: 1,
    needs_review: false,
  },
  title: "English 5.1",
  channels: 6,
  channel_layout: "5.1(side)",
  sample_rate: 48000,
  bit_depth: null,
  default: true,
  forced: false,
  roles: [],
  object_audio: false,
  video: null,
};

export function makeScan(): DiscScanResult {
  return {
    source: "/storage/Mintafilm",
    disc_kind: "bd",
    content_kind: "film",
    playlists: [
      {
        playlist_id: "00001",
        duration_seconds: 7200,
        chapters: [0, 600],
        segments: [{}],
        streams: [video, audio],
        angle_count: 1,
        seamless_branching: false,
        edition_group: null,
        edition_label: null,
        episode_number: null,
        recommended: true,
      },
    ],
    capabilities: {},
    fingerprint: "b".repeat(64),
    has_multiple_editions: false,
    has_seamless_branching: false,
    has_three_d: false,
    warnings: [],
  };
}

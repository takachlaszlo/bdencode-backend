export type JobState =
  | "QUEUED"
  | "SCANNING"
  | "AWAITING_SELECTION"
  | "READY"
  | "ENCODING"
  | "MUXING"
  | "QC"
  | "COMPARISON"
  | "UPLOADING"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED"
  | "NEEDS_REVIEW"
  | "UPLOAD_FAILED";

export type DiscType = "AUTO" | "BD" | "UHD";
export type ContentType = "FILM" | "CONCERT" | "ANIME" | "SERIES";
export type DetailLevel = "beginner" | "advanced" | "pro";
export type TrackAction = "copy" | "flac" | "omit";

export interface HealthResponse {
  status: string;
  database: string;
  schema_version: number;
  active_job_id: string | null;
  blocking_state: JobState | null;
  queued_jobs: number;
}

export interface CapabilitiesResponse {
  api_version: string;
  job_states: JobState[];
  terminal_states: JobState[];
  blocking_states: JobState[];
  transitions: Record<JobState, JobState[]>;
  input_video_codecs: string[];
  output_video_codecs: string[];
  disc_types: DiscType[];
  content_types: ContentType[];
  detail_levels: DetailLevel[];
  audio_actions: TrackAction[];
  constraints: {
    max_active_jobs?: number;
    queued_jobs_allowed?: boolean;
    cpu_budget_fraction?: number;
    supports_3d?: boolean;
    dolby_vision_retention?: boolean;
    hdr_modes?: string[];
    comparison_images?: string;
    [key: string]: unknown;
  };
}

export interface Job {
  id: string;
  name: string;
  source_path: string;
  work_path: string | null;
  output_path: string | null;
  disc_type: DiscType;
  content_type: ContentType;
  state: JobState;
  priority: number;
  settings: Record<string, unknown>;
  selection: Record<string, unknown> | null;
  requested_by: string | null;
  progress: number | null;
  status_message: string | null;
  error: string | null;
  resume_state: JobState | null;
  version: number;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface ListMeta {
  limit: number;
  offset: number;
  count: number;
}

export interface JobList {
  items: Job[];
  meta: ListMeta;
}

export interface JobCreate {
  source_path: string;
  name?: string;
  disc_type: DiscType;
  content_type: ContentType;
  priority: number;
  settings: Record<string, unknown>;
}

export interface SourceEntry {
  name: string;
  path: string;
  is_bluray: boolean;
}

export interface SourceBrowserResponse {
  roots: string[];
  path: string | null;
  entries: SourceEntry[];
}

export interface LanguageDecision {
  iso639_2t?: string | null;
  bcp47?: string | null;
  display_name?: string | null;
  confidence?: number | null;
  needs_review?: boolean;
  source?: string | null;
  warnings?: string[];
  [key: string]: unknown;
}

export interface VideoProperties {
  codec: string;
  width: number | null;
  height: number | null;
  frame_rate: string | null;
  field_order: string | null;
  bit_depth: number | null;
  pixel_format: string | null;
  color_primaries: string | null;
  color_transfer: string | null;
  color_matrix: string | null;
  color_range: string | null;
  chroma_location: string | null;
  hdr10: boolean;
  hdr10_static?: {
    mastering_display?: string | null;
    max_cll?: number | null;
    max_fall?: number | null;
  };
  dolby_vision: boolean;
  dolby_vision_profile?: number | null;
  hdr10_base_layer: boolean;
  hdr10_plus: boolean;
  three_d: boolean;
}

export interface MediaStream {
  id: string;
  index: number;
  pid: number | null;
  kind: "video" | "audio" | "subtitle";
  codec: string;
  codec_profile: string | null;
  language: LanguageDecision | null;
  title: string | null;
  channels: number | null;
  channel_layout: string | null;
  sample_rate: number | null;
  bit_depth: number | null;
  default: boolean;
  forced: boolean;
  roles: string[];
  object_audio: boolean;
  video: VideoProperties | null;
}

export interface Playlist {
  playlist_id: string;
  duration_seconds: number;
  chapters: number[];
  segments: Array<Record<string, unknown>>;
  streams: MediaStream[];
  angle_count: number;
  seamless_branching: boolean;
  edition_group: string | null;
  edition_label: string | null;
  episode_number: number | null;
  recommended: boolean;
}

export interface DiscScanResult {
  source: string;
  disc_kind: "bd" | "uhd";
  content_kind: "film" | "concert" | "anime" | "series";
  playlists: Playlist[];
  capabilities: Record<string, unknown>;
  fingerprint: string;
  has_multiple_editions: boolean;
  has_seamless_branching: boolean;
  has_three_d: boolean;
  warnings: string[];
}

export interface Scan {
  id: string;
  job_id: string;
  source_path: string;
  status: "PENDING" | "RUNNING" | "AWAITING_SELECTION" | "COMPLETED" | "FAILED";
  result: DiscScanResult | Record<string, never>;
  error: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface ScanList {
  items: Scan[];
  meta: ListMeta;
}

export type ArtifactKind =
  | "LOG"
  | "MANIFEST"
  | "MEDIAINFO"
  | "MKVINFO"
  | "VIDEO_COMPARISON"
  | "AUDIO_COMPARISON"
  | "SPECTROGRAM"
  | "REPORT"
  | "BBCODE"
  | "OUTPUT"
  | "OTHER";

export interface Artifact {
  id: string;
  job_id: string;
  scan_id: string | null;
  kind: ArtifactKind;
  name: string;
  path: string;
  mime_type: string | null;
  sha256: string | null;
  size_bytes: number | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface ArtifactList {
  items: Artifact[];
  meta: ListMeta;
}

export interface EventRecord {
  id: number;
  job_id: string | null;
  scan_id: string | null;
  kind: string;
  state_from: JobState | null;
  state_to: JobState | null;
  message: string | null;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface EventList {
  items: EventRecord[];
  after_id: number;
}

export interface FieldSpec {
  name: string;
  group: string;
  introduced_at: DetailLevel;
  required: boolean;
  default: unknown;
  value_type: "enum" | "number" | "integer" | "boolean" | "string" | "object";
  minimum: number | null;
  maximum: number | null;
  choices: string[];
  description: string;
}

export interface ProfileSchemaResponse {
  encoder: "x264" | "x265";
  detail_level: DetailLevel;
  fields: FieldSpec[];
}

export interface ProfileRecommendationResponse {
  source: string;
  requires_operator_confirmation: boolean;
  settings: Record<string, unknown>;
}

export interface TrackSelection {
  stream_id: string;
  action: TrackAction;
  language: string | null;
  name: string | null;
  default: boolean;
  forced: boolean;
  order: number;
}

export interface SelectionPayload {
  playlist_id: string;
  angle: number;
  output_name: string;
  video: {
    detail_level: DetailLevel;
    temporal_filter: string;
    crop: { left: number; top: number; right: number; bottom: number };
    settings: Record<string, unknown>;
  };
  tracks: TrackSelection[];
  upload_images: boolean;
  dual_type_match: boolean;
}

export interface SelectionValidation {
  valid: boolean;
  playlist_id: string;
  encoder: "x264" | "x265";
  settings: Record<string, unknown>;
  ffmpeg_video_args: string[];
  crop: { left: number; top: number; right: number; bottom: number };
  temporal_filter: string;
  advisory_warnings: string[];
}

export interface VideoComparisonPair {
  category: "I" | "P" | "B";
  presentation_index: number;
  encoded_pts_seconds: string | number;
  reference_pts_seconds: string | number;
  encoded_pict_type: "I" | "P" | "B";
  source_pict_type: "I" | "P" | "B" | null;
  dual_type_match: boolean;
  reference_png: string;
  encode_png: string;
  reference_sha256?: string;
  encode_sha256?: string;
  reference_sdr_png?: string;
  encode_sdr_png?: string;
  [key: string]: unknown;
}

export interface VideoComparisonManifest {
  schema_version: number;
  categorization: string;
  alignment: string;
  pairs: VideoComparisonPair[];
  counts: Record<string, number>;
}

export interface AudioComparisonTrack {
  stream_id: string;
  action: TrackAction;
  source_spectrum: string;
  encode_spectrum: string;
  decoded_pcm_sha256_match: boolean;
  delay_within_one_sample: boolean;
  comparison: Record<string, unknown>;
  source_probe: Record<string, unknown>;
  encode_probe: Record<string, unknown>;
  [key: string]: unknown;
}

export interface AudioComparisonManifest {
  schema_version: number;
  tracks: AudioComparisonTrack[];
}

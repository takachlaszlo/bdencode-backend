import type { AutoCrfConfig, DetailLevel, DynamicHdrMode, ImageUploadProvider, SelectionPayload, TrackAction } from "./api/types";

type Crop = SelectionPayload["video"]["crop"];

export interface StoredTrackSelection {
  stream_id: string;
  action: TrackAction;
  language?: string | null;
  name?: string | null;
  default?: boolean;
  forced?: boolean;
  subtitle_kind?: "unknown" | "full" | "forced" | null;
  order?: number;
}

export interface StoredSelection {
  playlistId: string | null;
  angle: number | null;
  outputName: string | null;
  detailLevel: DetailLevel | null;
  temporalFilter: string | null;
  crop: Crop | null;
  settings: Record<string, unknown>;
  tracks: StoredTrackSelection[];
  uploadImages: boolean | null;
  imageUploadProvider: ImageUploadProvider | null;
  dualTypeMatch: boolean | null;
  autoCrf: AutoCrfConfig | null;
  dynamicHdr: DynamicHdrMode | null;
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function boolean(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function normalizeAutoCrf(value: unknown): AutoCrfConfig | null {
  const raw = record(value);
  if (!raw || raw.enabled !== true) return null;
  const target = typeof raw.target_vmaf === "number" && Number.isFinite(raw.target_vmaf) ? raw.target_vmaf : 95;
  const result: AutoCrfConfig = { enabled: true, target_vmaf: target };
  for (const key of ["min_crf", "max_crf", "samples", "sample_seconds", "max_iterations", "tolerance"] as const) {
    const item = raw[key];
    if (typeof item === "number" && Number.isFinite(item)) result[key] = item;
  }
  if (raw.metric === "mean" || raw.metric === "harmonic_mean" || raw.metric === "percentile_1") result.metric = raw.metric;
  if (typeof raw.probe_preset === "string" && raw.probe_preset) result.probe_preset = raw.probe_preset;
  return result;
}

function normalizeDynamicHdr(value: unknown): DynamicHdrMode | null {
  return value === "discard" || value === "auto" || value === "hdr10plus" || value === "dolby_vision" ? value : null;
}

function normalizePlaylistId(value: unknown): string | null {
  if (typeof value !== "string" && typeof value !== "number") return null;
  const normalized = String(value).toLowerCase().replace(/\.mpls$/, "");
  return /^\d+$/.test(normalized) ? normalized.padStart(5, "0") : null;
}

function normalizeDetailLevel(value: unknown): DetailLevel | null {
  return value === "beginner" || value === "advanced" || value === "pro" ? value : null;
}

function normalizeImageUploadProvider(value: unknown): ImageUploadProvider | null {
  return value === "auto" || value === "imgbb" || value === "catbox" || value === "freeimage"
    ? value
    : null;
}

function normalizeCrop(value: unknown): Crop | null {
  const candidate = record(value);
  if (!candidate) return null;
  const sides = ["left", "top", "right", "bottom"] as const;
  if (!sides.every((side) => Number.isInteger(candidate[side]) && Number(candidate[side]) >= 0)) {
    return null;
  }
  return {
    left: Number(candidate.left),
    top: Number(candidate.top),
    right: Number(candidate.right),
    bottom: Number(candidate.bottom),
  };
}

function normalizeTrackAction(value: unknown): TrackAction | null {
  if (typeof value !== "string") return null;
  const normalized = value.toLowerCase();
  return normalized === "copy"
    || normalized === "flac"
    || normalized === "ac3"
    || normalized === "eac3"
    || normalized === "dts"
    || normalized === "omit"
    ? normalized
    : null;
}

function normalizeTracks(value: unknown): StoredTrackSelection[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((raw, index) => {
    const candidate = record(raw);
    const streamId = text(candidate?.stream_id);
    const action = normalizeTrackAction(candidate?.action);
    if (!candidate || !streamId || !action) return [];
    const normalized: StoredTrackSelection = { stream_id: streamId, action };
    if (candidate.language === null || text(candidate.language)) {
      normalized.language = candidate.language === null ? null : text(candidate.language);
    }
    if (candidate.name === null || text(candidate.name)) {
      normalized.name = candidate.name === null ? null : text(candidate.name);
    }
    if (typeof candidate.default === "boolean") normalized.default = candidate.default;
    if (typeof candidate.forced === "boolean") normalized.forced = candidate.forced;
    if (
      candidate.subtitle_kind === null
      || candidate.subtitle_kind === "unknown"
      || candidate.subtitle_kind === "full"
      || candidate.subtitle_kind === "forced"
    ) {
      normalized.subtitle_kind = candidate.subtitle_kind;
    }
    normalized.order = typeof candidate.order === "number"
      && Number.isInteger(candidate.order)
      && candidate.order >= 0
      ? candidate.order
      : index;
    return [normalized];
  });
}

/**
 * Convert the backend's intentionally open JsonObject into safe UI defaults.
 * Legacy `video.overrides` remains readable, while malformed fields are ignored
 * so a NEEDS_REVIEW job can always be opened and corrected in the wizard.
 */
export function normalizeStoredSelection(value: unknown): StoredSelection | null {
  const selection = record(value);
  if (!selection) return null;
  const video = record(selection.video);
  const canonicalSettings = record(video?.settings);
  const legacySettings = record(video?.overrides);
  const angle = typeof selection.angle === "number"
    && Number.isInteger(selection.angle)
    && selection.angle >= 1
    ? selection.angle
    : null;

  return {
    playlistId: normalizePlaylistId(selection.playlist_id),
    angle,
    outputName: text(selection.output_name),
    detailLevel: normalizeDetailLevel(video?.detail_level),
    temporalFilter: text(video?.temporal_filter) ?? text(selection.temporal_filter),
    crop: normalizeCrop(video?.crop) ?? normalizeCrop(selection.crop),
    settings: { ...(canonicalSettings ?? legacySettings ?? {}) },
    tracks: normalizeTracks(selection.tracks),
    uploadImages: boolean(selection.upload_images),
    imageUploadProvider: normalizeImageUploadProvider(selection.image_upload_provider),
    dualTypeMatch: boolean(selection.dual_type_match),
    autoCrf: normalizeAutoCrf(video?.auto_crf),
    dynamicHdr: normalizeDynamicHdr(video?.dynamic_hdr),
  };
}

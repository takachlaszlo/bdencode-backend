import type { DiscScanResult, VideoProperties } from "./api/types";

export type SourceColorField =
  | "primaries"
  | "transfer"
  | "matrix"
  | "range"
  | "chroma_location";

export interface SourceColorMetadata {
  primaries: string;
  transfer: string;
  matrix: string;
  range: string;
  chroma_location: string;
}

export const SOURCE_COLOR_FIELD_LABELS: Record<SourceColorField, string> = {
  primaries: "Színprimerek",
  transfer: "Átviteli karakterisztika",
  matrix: "Mátrixegyütthatók",
  range: "Jeltartomány",
  chroma_location: "Chroma-elhelyezés",
};

const BLOCKING_SOURCE_FIELDS = ["primaries", "transfer", "matrix"] as const;
const ALL_SOURCE_FIELDS: SourceColorField[] = [
  ...BLOCKING_SOURCE_FIELDS,
  "range",
  "chroma_location",
];

function normalize(value: string | null | undefined): string | null {
  const normalized = value?.trim().toLowerCase();
  return normalized || null;
}

function normalizeRange(value: string | null | undefined): string | null {
  const normalized = normalize(value);
  if (normalized === "tv" || normalized === "mpeg") return "limited";
  if (normalized === "pc" || normalized === "jpeg") return "full";
  return normalized;
}

function normalizeMatrix(value: string | null | undefined): string | null {
  const normalized = normalize(value);
  if (normalized === "bt2020ncl") return "bt2020nc";
  if (normalized === "bt2020cl") return "bt2020c";
  return normalized;
}

export function missingSourceColorFields(video: VideoProperties | null | undefined): SourceColorField[] {
  if (!video) return [...ALL_SOURCE_FIELDS];
  return ALL_SOURCE_FIELDS.filter((field) => {
    if (field === "primaries") return !normalize(video.color_primaries);
    if (field === "transfer") return !normalize(video.color_transfer);
    if (field === "matrix") return !normalize(video.color_matrix);
    if (field === "range") return !normalize(video.color_range);
    return !normalize(video.chroma_location);
  });
}

export function blockingSourceColorFields(video: VideoProperties | null | undefined): SourceColorField[] {
  const missing = new Set(missingSourceColorFields(video));
  return BLOCKING_SOURCE_FIELDS.filter((field) => missing.has(field));
}

export function hasSafeSourceColorRecommendation(
  video: VideoProperties | null | undefined,
  discKind: DiscScanResult["disc_kind"],
): boolean {
  if (!video) return false;
  if (discKind === "uhd") return Boolean(video.hdr10);
  return !video.hdr10
    && (video.width ?? 0) >= 1280
    && (video.height ?? 0) >= 720
    && video.bit_depth === 8;
}

export function suggestedSourceColor(
  video: VideoProperties | null | undefined,
  discKind: DiscScanResult["disc_kind"],
): SourceColorMetadata {
  const hdr10 = discKind === "uhd" && Boolean(video?.hdr10);
  const safeRecommendation = hasSafeSourceColorRecommendation(video, discKind);
  const standard: SourceColorMetadata = hdr10
    ? {
        primaries: "bt2020",
        transfer: "smpte2084",
        matrix: "bt2020nc",
        range: "limited",
        chroma_location: "left",
      }
    : {
        primaries: "bt709",
        transfer: "bt709",
        matrix: "bt709",
        range: "limited",
        chroma_location: "left",
      };

  return {
    primaries: normalize(video?.color_primaries) ?? (safeRecommendation ? standard.primaries : ""),
    transfer: normalize(video?.color_transfer) ?? (safeRecommendation ? standard.transfer : ""),
    matrix: normalizeMatrix(video?.color_matrix) ?? (safeRecommendation ? standard.matrix : ""),
    range: normalizeRange(video?.color_range) ?? (safeRecommendation ? standard.range : ""),
    chroma_location: normalize(video?.chroma_location) ?? (safeRecommendation ? standard.chroma_location : ""),
  };
}

export function parseSourceColor(value: unknown): SourceColorMetadata | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const candidate = value as Record<string, unknown>;
  const fields = ALL_SOURCE_FIELDS;
  if (!fields.every((field) => typeof candidate[field] === "string" && candidate[field].trim())) {
    return null;
  }
  return Object.fromEntries(fields.map((field) => [field, String(candidate[field]).trim().toLowerCase()])) as unknown as SourceColorMetadata;
}

export interface SourceColorApiIssue {
  missing: SourceColorField[];
  suggested: SourceColorMetadata | null;
}

function isSourceColorField(value: unknown): value is SourceColorField {
  return value === "primaries"
    || value === "transfer"
    || value === "matrix"
    || value === "range"
    || value === "chroma_location";
}

/** Reads the structured validation response while retaining compatibility with the first backend release. */
export function sourceColorIssueFromPayload(payload: unknown): SourceColorApiIssue | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const response = payload as Record<string, unknown>;
  const detail = typeof response.detail === "string" ? response.detail.toLowerCase() : "";
  if (response.code !== "source_color_confirmation_required"
    && !detail.includes("source color metadata is incomplete")) {
    return null;
  }

  const context = response.context && typeof response.context === "object" && !Array.isArray(response.context)
    ? response.context as Record<string, unknown>
    : response;
  const missing = Array.isArray(context.missing_fields)
    ? context.missing_fields.filter(isSourceColorField)
    : Array.isArray(context.missing)
      ? context.missing.filter(isSourceColorField)
      : [];
  return {
    missing,
    suggested: parseSourceColor(context.suggested) ?? parseSourceColor(context.safe_defaults),
  };
}

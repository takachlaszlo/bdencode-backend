import { describe, expect, it } from "vitest";
import { normalizeStoredSelection } from "./selection";

describe("normalizeStoredSelection", () => {
  it("turns a malformed backend JsonObject into safe editable defaults", () => {
    expect(normalizeStoredSelection({ unexpected: true })).toEqual({
      playlistId: null,
      angle: null,
      outputName: null,
      detailLevel: null,
      temporalFilter: null,
      crop: null,
      settings: {},
      tracks: [],
      uploadImages: null,
      imageUploadProvider: null,
      dualTypeMatch: null,
      autoCrf: null,
      dynamicHdr: null,
    });
  });

  it("preserves supported legacy overrides and top-level crop/filter fields", () => {
    expect(normalizeStoredSelection({
      playlist_id: "1.mpls",
      angle: 2,
      output_name: "Legacy.Encode",
      video: {
        detail_level: "advanced",
        overrides: { crf: 16, preset: "slower" },
      },
      crop: { left: 2, top: 4, right: 6, bottom: 8 },
      temporal_filter: "progressive",
      tracks: [{
        stream_id: "audio:4352",
        action: "copy",
        language: "eng",
        name: null,
        default: true,
        forced: false,
        order: 0,
      }],
      upload_images: false,
      image_upload_provider: "catbox",
      dual_type_match: true,
    })).toEqual({
      playlistId: "00001",
      angle: 2,
      outputName: "Legacy.Encode",
      detailLevel: "advanced",
      temporalFilter: "progressive",
      crop: { left: 2, top: 4, right: 6, bottom: 8 },
      settings: { crf: 16, preset: "slower" },
      tracks: [{
        stream_id: "audio:4352",
        action: "copy",
        language: "eng",
        name: null,
        default: true,
        forced: false,
        order: 0,
      }],
      uploadImages: false,
      imageUploadProvider: "catbox",
      dualTypeMatch: true,
      autoCrf: null,
      dynamicHdr: null,
    });
  });

  it.each(["ac3", "eac3", "dts"] as const)("preserves the %s audio target", (action) => {
    const value = normalizeStoredSelection({
      playlist_id: "00001",
      tracks: [{ stream_id: "audio:4352", action }],
    });

    expect(value?.tracks).toEqual([{ stream_id: "audio:4352", action, order: 0 }]);
  });

  it("drops an unknown audio action instead of restoring an unsafe value", () => {
    const value = normalizeStoredSelection({
      playlist_id: "00001",
      tracks: [{ stream_id: "audio:4352", action: "mp3" }],
    });

    expect(value?.tracks).toEqual([]);
  });
});

describe("normalizeStoredSelection quality options", () => {
  it("restores an enabled automatic CRF search and the dynamic HDR policy", () => {
    const stored = normalizeStoredSelection({
      video: {
        auto_crf: { enabled: true, target_vmaf: 94.5, min_crf: 14, max_crf: 24, metric: "harmonic_mean", junk: 1 },
        dynamic_hdr: "hdr10plus",
      },
    });
    expect(stored?.autoCrf).toEqual({
      enabled: true,
      target_vmaf: 94.5,
      min_crf: 14,
      max_crf: 24,
      metric: "harmonic_mean",
    });
    expect(stored?.dynamicHdr).toBe("hdr10plus");
  });

  it("ignores disabled, malformed or unknown values", () => {
    expect(normalizeStoredSelection({ video: { auto_crf: { enabled: false, target_vmaf: 95 } } })?.autoCrf).toBeNull();
    expect(normalizeStoredSelection({ video: { auto_crf: "yes" } })?.autoCrf).toBeNull();
    expect(normalizeStoredSelection({ video: { auto_crf: { enabled: true } } })?.autoCrf).toEqual({
      enabled: true,
      target_vmaf: 95,
    });
    expect(normalizeStoredSelection({ video: { dynamic_hdr: "always" } })?.dynamicHdr).toBeNull();
  });
});

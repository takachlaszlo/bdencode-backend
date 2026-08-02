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
      dualTypeMatch: null,
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
      dualTypeMatch: true,
    });
  });
});

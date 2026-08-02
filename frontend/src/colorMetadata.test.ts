import { describe, expect, it } from "vitest";
import {
  blockingSourceColorFields,
  hasSafeSourceColorRecommendation,
  missingSourceColorFields,
  sourceColorIssueFromPayload,
  suggestedSourceColor,
} from "./colorMetadata";
import { makeScan } from "./test/fixtures";

describe("source color metadata", () => {
  it("offers BT.709 only for an 8-bit HD SDR Blu-ray and preserves known aliases", () => {
    const video = makeScan().playlists[0].streams[0].video;
    if (!video) throw new Error("A teszt videósávja hiányzik");

    const incomplete = {
      ...video,
      color_primaries: null,
      color_transfer: null,
      color_matrix: null,
      color_range: "tv",
    };

    expect(hasSafeSourceColorRecommendation(incomplete, "bd")).toBe(true);
    expect(suggestedSourceColor(incomplete, "bd")).toEqual({
      primaries: "bt709",
      transfer: "bt709",
      matrix: "bt709",
      range: "limited",
      chroma_location: "left",
    });
    expect(blockingSourceColorFields(incomplete)).toEqual(["primaries", "transfer", "matrix"]);
  });

  it("does not prefill a color standard for an SD Blu-ray or SDR UHD source", () => {
    const video = makeScan().playlists[0].streams[0].video;
    if (!video) throw new Error("A teszt videósávja hiányzik");
    const incompleteSd = {
      ...video,
      width: 720,
      height: 576,
      color_primaries: null,
      color_transfer: null,
      color_matrix: null,
      color_range: null,
      chroma_location: null,
    };

    expect(hasSafeSourceColorRecommendation(incompleteSd, "bd")).toBe(false);
    expect(suggestedSourceColor(incompleteSd, "bd")).toEqual({
      primaries: "",
      transfer: "",
      matrix: "",
      range: "",
      chroma_location: "",
    });
    expect(hasSafeSourceColorRecommendation({ ...incompleteSd, width: 3840, height: 2160 }, "uhd")).toBe(false);
    expect(missingSourceColorFields(incompleteSd)).toEqual([
      "primaries",
      "transfer",
      "matrix",
      "range",
      "chroma_location",
    ]);
  });

  it("uses BT.2020/PQ for a confirmed HDR10 UHD source", () => {
    const video = makeScan().playlists[0].streams[0].video;
    if (!video) throw new Error("A teszt videósávja hiányzik");
    const hdr10 = {
      ...video,
      width: 3840,
      height: 2160,
      bit_depth: 10,
      hdr10: true,
      color_primaries: null,
      color_transfer: null,
      color_matrix: null,
    };

    expect(hasSafeSourceColorRecommendation(hdr10, "uhd")).toBe(true);
    expect(suggestedSourceColor(hdr10, "uhd")).toEqual(expect.objectContaining({
      primaries: "bt2020",
      transfer: "smpte2084",
      matrix: "bt2020nc",
    }));
  });

  it("parses exact missing fields and safe defaults from the structured 422 payload", () => {
    expect(sourceColorIssueFromPayload({
      detail: "A forrás színinformációja hiányos.",
      code: "source_color_confirmation_required",
      context: {
        missing_fields: ["primaries", "transfer", "matrix", "range", "unexpected"],
        safe_defaults: {
          primaries: "bt709",
          transfer: "bt709",
          matrix: "bt709",
          range: "limited",
          chroma_location: "left",
        },
      },
    })).toEqual({
      missing: ["primaries", "transfer", "matrix", "range"],
      suggested: {
        primaries: "bt709",
        transfer: "bt709",
        matrix: "bt709",
        range: "limited",
        chroma_location: "left",
      },
    });
  });
});

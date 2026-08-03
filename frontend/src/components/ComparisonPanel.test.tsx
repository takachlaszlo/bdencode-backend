import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AudioComparisonManifest, VideoComparisonManifest } from "../api/types";
import { makeArtifact } from "../test/fixtures";
import { renderApp } from "../test/render";
import { ComparisonPanel } from "./ComparisonPanel";

function jsonResponse(payload: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: vi.fn().mockResolvedValue(payload),
  } as unknown as Response;
}

describe("ComparisonPanel", () => {
  const videoManifest: VideoComparisonManifest = {
    schema_version: 2,
    categorization: "dual-decoder",
    alignment: "presentation-index-and-pts",
    counts: { I: 1, P: 0, B: 0 },
    metrics: {
      backend: "ffmpeg-sampled-ssim-psnr",
      scope: "selected_ipb_native_png_pairs",
      sample_count: 5,
      full_title_measurement: false,
      aggregate: {
        ssim_all_mean: 0.9987654,
        psnr_average_db_mean: 42.345,
      },
    },
    pairs: [
      {
        category: "I",
        presentation_index: 12,
        encoded_pts_seconds: "0.500",
        reference_pts_seconds: "0.500",
        encoded_pict_type: "I",
        source_pict_type: "I",
        dual_type_match: true,
        reference_png: "i-source.png",
        encode_png: "i-encode.png",
      },
    ],
  };
  const audioManifest: AudioComparisonManifest = {
    schema_version: 1,
    tracks: [
      {
        stream_id: "audio:4352",
        action: "flac",
        source_spectrum: "audio-source.png",
        encode_spectrum: "audio-encode.png",
        decoded_pcm_sha256_match: true,
        delay_within_one_sample: true,
        comparison: { sample_count_delta: 0 },
        source_probe: {},
        encode_probe: {},
      },
    ],
  };

  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        return Promise.resolve(
          jsonResponse(url.includes("audio-manifest") ? audioManifest : videoManifest),
        );
      }),
    );
  });

  it("renders aligned frame controls and registered audio spectrograms", async () => {
    const artifacts = [
      makeArtifact({ id: "video-manifest", kind: "VIDEO_COMPARISON", name: "video-comparison.json", mime_type: "application/json" }),
      makeArtifact({ id: "audio-manifest", kind: "AUDIO_COMPARISON", name: "audio-comparison.json", mime_type: "application/json" }),
      makeArtifact({ id: "i-source", kind: "VIDEO_COMPARISON", name: "i-source.png", mime_type: "image/png" }),
      makeArtifact({ id: "i-encode", kind: "VIDEO_COMPARISON", name: "i-encode.png", mime_type: "image/png" }),
      makeArtifact({ id: "audio-source", kind: "SPECTROGRAM", name: "audio-source.png", mime_type: "image/png" }),
      makeArtifact({ id: "audio-encode", kind: "SPECTROGRAM", name: "audio-encode.png", mime_type: "image/png" }),
    ];
    const user = userEvent.setup();

    renderApp(<ComparisonPanel artifacts={artifacts} />);

    expect(await screen.findByText("I-frame")).toBeInTheDocument();
    expect(screen.getByText("Mintavételezett képmetrikák")).toBeInTheDocument();
    expect(screen.getByText("SSIM: 0.998765")).toBeInTheDocument();
    expect(screen.getByText("PSNR: 42.34 dB")).toBeInTheDocument();
    expect(await screen.findByText("PCM egyezik")).toBeInTheDocument();
    expect(screen.getByText("Időzítés rendben")).toBeInTheDocument();
    expect(screen.getByAltText("audio:4352 source spektrum")).toHaveAttribute(
      "src",
      "/encoder/api/v1/artifacts/audio-source/content",
    );
    expect(screen.getByAltText("audio:4352 encode spektrum")).toHaveAttribute(
      "src",
      "/encoder/api/v1/artifacts/audio-encode/content",
    );

    const slider = screen.getByRole("slider", { name: "Source és encode elválasztása" });
    fireEvent.change(slider, { target: { value: "72" } });
    expect(screen.getByAltText("I-frame forrás")).toHaveStyle({
      clipPath: "inset(0 28% 0 0)",
    });

    await user.click(screen.getByRole("button", { name: "A/B" }));
    expect(screen.getByAltText("I-frame forrás")).toBeInTheDocument();
    expect(screen.getByAltText("I-frame encode")).toBeInTheDocument();
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  });

  it("shows a real warning instead of broken images when a frame artifact is absent", async () => {
    renderApp(
      <ComparisonPanel
        artifacts={[
          makeArtifact({ id: "video-manifest", kind: "VIDEO_COMPARISON", name: "video-comparison.json", mime_type: "application/json" }),
        ]}
      />,
    );

    expect(
      await screen.findByText(/I-frame egyik PNG melléklete hiányzik/),
    ).toBeInTheDocument();
  });

  it("labels lossy audio as intentional instead of reporting a PCM integrity error", async () => {
    const lossyManifest: AudioComparisonManifest = {
      schema_version: 2,
      tracks: [{
        ...audioManifest.tracks[0],
        action: "eac3",
        decoded_pcm_sha256_match: null,
        decoded_pcm_sha256_required: false,
        timing_within_tolerance: true,
        verification_mode: "lossy_transcode",
      }],
    };
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse(lossyManifest))));

    renderApp(
      <ComparisonPanel
        artifacts={[
          makeArtifact({ id: "audio-manifest", kind: "AUDIO_COMPARISON", name: "audio-comparison.json", mime_type: "application/json" }),
        ]}
      />,
    );

    expect(await screen.findByText("E-AC-3 konverzió")).toBeInTheDocument();
    expect(screen.getByText("Veszteséges cél · PCM hash nem elvárt")).toBeInTheDocument();
    expect(screen.queryByText("PCM eltérés")).not.toBeInTheDocument();
  });

  it("shows a neutral same-frame label when source picture type is not applicable", async () => {
    const transformedManifest: VideoComparisonManifest = {
      ...videoManifest,
      pairs: [{
        ...videoManifest.pairs[0],
        source_pict_type: null,
        dual_type_match: false,
      }],
    };
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse(transformedManifest))));

    renderApp(
      <ComparisonPanel
        artifacts={[
          makeArtifact({ id: "video-manifest", kind: "VIDEO_COMPARISON", name: "video-comparison.json", mime_type: "application/json" }),
          makeArtifact({ id: "i-source", kind: "VIDEO_COMPARISON", name: "i-source.png", mime_type: "image/png" }),
          makeArtifact({ id: "i-encode", kind: "VIDEO_COMPARISON", name: "i-encode.png", mime_type: "image/png" }),
        ]}
      />,
    );

    expect(await screen.findByText("Source képtípus nem értelmezhető · azonos frame")).toBeInTheDocument();
    expect(screen.queryByText(/eltérő típus/)).not.toBeInTheDocument();
  });
});

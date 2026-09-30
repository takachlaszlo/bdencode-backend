import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../api/client";
import type { PlayerInfo } from "../api/types";
import { renderApp } from "../test/render";
import { PlayerPanel } from "./PlayerPanel";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      playerInfo: vi.fn(),
      createPreview: vi.fn(),
      deletePreview: vi.fn(),
    },
  };
});

function info(overrides: Partial<PlayerInfo> = {}): PlayerInfo {
  return {
    duration_seconds: 7200,
    video: { codec: "hevc", width: 3840, height: 2160, pix_fmt: "yuv420p10le", hdr: true, color_transfer: "smpte2084" },
    audio: [{ codec: "eac3", channels: 6, language: "eng", title: null }],
    subtitles: 2,
    chapters: [
      { start_seconds: 0, title: "Nyitány" },
      { start_seconds: 1800, title: "Fordulat" },
    ],
    previews: [],
    limits: { min_duration_seconds: 5, max_duration_seconds: 30, heights: [360, 480, 720] },
    ...overrides,
  };
}

describe("PlayerPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.playerInfo).mockResolvedValue(info());
  });

  it("shows the MKV summary, chapters and the HDR tone-map notice", async () => {
    renderApp(<PlayerPanel jobId="job-1" />);

    expect(await screen.findByText("Beépített lejátszó")).toBeInTheDocument();
    expect(screen.getByText("Nyitány")).toBeInTheDocument();
    expect(screen.getByText("Fordulat")).toBeInTheDocument();
    expect(screen.getByText(/HEVC · 3840×2160/)).toBeInTheDocument();
    expect(screen.getByText(/EAC3 6ch · eng/)).toBeInTheDocument();
    expect(screen.getByText(/SDR-re tone-map-elt/)).toBeInTheDocument();
    expect(screen.getByText(/Válassz kezdőpontot/)).toBeInTheDocument();
    expect(screen.queryByLabelText("A kész MKV kivonata")).not.toBeInTheDocument();
  });

  it("cuts an excerpt from the chosen chapter and plays it", async () => {
    const user = userEvent.setup();
    vi.mocked(api.createPreview).mockResolvedValue({
      name: "preview-1800s-20s-720p-abcdef0123.mp4",
      start_seconds: 1800,
      duration_seconds: 20,
      height: 720,
      size_bytes: 4_000_000,
      created_at: 1,
      created: true,
    });
    renderApp(<PlayerPanel jobId="job-1" />);

    await user.click(await screen.findByRole("button", { name: /Fordulat/ }));
    expect(screen.getByText(/Kezdőpont: 30:00 \/ 2:00:00/)).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Kivonat felbontása"), "480");
    await user.click(screen.getByRole("button", { name: "Kivonat készítése" }));

    await waitFor(() =>
      expect(api.createPreview).toHaveBeenCalledWith("job-1", {
        start_seconds: 1800,
        duration_seconds: 20,
        height: 480,
      }),
    );
    const video = await screen.findByLabelText("A kész MKV kivonata");
    expect(video.tagName).toBe("VIDEO");
    expect(video).toHaveAttribute("src", expect.stringContaining("/jobs/job-1/previews/preview-1800s-20s-720p-abcdef0123.mp4"));
    expect(video).toHaveAttribute("controls");
  });

  it("lists cached excerpts, plays one and deletes it", async () => {
    const user = userEvent.setup();
    const name = "preview-60s-20s-360p-0123456789.mp4";
    vi.mocked(api.playerInfo).mockResolvedValue(
      info({ previews: [{ name, start_seconds: 60, duration_seconds: 20, height: 360, size_bytes: 1_500_000, created_at: 1 }] }),
    );
    vi.mocked(api.deletePreview).mockResolvedValue(undefined);
    renderApp(<PlayerPanel jobId="job-1" />);

    await user.click(await screen.findByRole("button", { name: /1:00 · 20 s · 360p/ }));
    expect(await screen.findByLabelText("A kész MKV kivonata")).toHaveAttribute("src", expect.stringContaining(name));

    await user.click(screen.getByRole("button", { name: "Kivonat törlése (1:00)" }));
    await waitFor(() => expect(api.deletePreview).toHaveBeenCalledWith("job-1", name));
    await waitFor(() => expect(screen.queryByLabelText("A kész MKV kivonata")).not.toBeInTheDocument());
  });

  it("steps the start point by minutes and clamps it to the film", async () => {
    const user = userEvent.setup();
    renderApp(<PlayerPanel jobId="job-1" />);

    await screen.findByText("Beépített lejátszó");
    await user.click(screen.getByRole("button", { name: "+1 perc" }));
    await user.click(screen.getByRole("button", { name: "+1 perc" }));
    expect(screen.getByText(/Kezdőpont: 2:00 /)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "−1 perc" }));
    expect(screen.getByText(/Kezdőpont: 1:00 /)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "−1 perc" }));
    await user.click(screen.getByRole("button", { name: "−1 perc" }));
    expect(screen.getByText(/Kezdőpont: 0:00 /)).toBeInTheDocument();
  });

  it("reports why an excerpt could not be made", async () => {
    const user = userEvent.setup();
    vi.mocked(api.createPreview).mockRejectedValue(new ApiError(502, "ffmpeg could not create the excerpt", null));
    renderApp(<PlayerPanel jobId="job-1" />);

    await user.click(await screen.findByRole("button", { name: "Kivonat készítése" }));
    expect(await screen.findByText("A kivonat nem készült el")).toBeInTheDocument();
    expect(screen.getByText("ffmpeg could not create the excerpt")).toBeInTheDocument();
  });

  it("explains a missing ffmpeg and an unfinished job", async () => {
    vi.mocked(api.playerInfo).mockRejectedValueOnce(new ApiError(503, "unavailable", null));
    const first = renderApp(<PlayerPanel jobId="job-1" />);
    expect(await screen.findByText("A lejátszó ezen a szerveren nem érhető el")).toBeInTheDocument();
    expect(screen.getByText(/ffmpeg és az ffprobe szükséges/)).toBeInTheDocument();
    first.unmount();

    vi.mocked(api.playerInfo).mockRejectedValueOnce(new ApiError(409, "the job has no finished MKV yet", null));
    renderApp(<PlayerPanel jobId="job-2" />);
    expect(await screen.findByText("A lejátszó még nem használható")).toBeInTheDocument();
    expect(screen.getByText(/the job has no finished MKV yet/)).toBeInTheDocument();
  });
});

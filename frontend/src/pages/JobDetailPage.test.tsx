import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../api/client";
import { makeJob } from "../test/fixtures";
import { renderApp } from "../test/render";
import { JobDetailPage } from "./JobDetailPage";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      job: vi.fn(),
      scans: vi.fn(),
      artifacts: vi.fn(),
      events: vi.fn(),
      cancelJob: vi.fn(),
      retryJob: vi.fn(),
      retryUpload: vi.fn(),
      resumeJob: vi.fn(),
    },
  };
});

const failedJob = makeJob({
  state: "FAILED",
  progress: 0.55,
  status_message: "A muxolás sikertelen",
  error: "FileNotFoundError: /job/work/chapters.xml",
  resume_state: "MUXING",
});

const retriedJob = makeJob({
  state: "MUXING",
  progress: 0.78,
  status_message: "Folytatás az érvényes checkpointoktól",
  error: null,
  resume_state: null,
  version: 2,
});

function renderFailedJob() {
  return renderApp(
    <Routes>
      <Route path="/jobs/:jobId" element={<JobDetailPage />} />
    </Routes>,
    "/jobs/job-1",
  );
}

describe("JobDetailPage failed-job retry", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.job).mockResolvedValue(failedJob);
    vi.mocked(api.scans).mockResolvedValue({ items: [], meta: { limit: 100, offset: 0, count: 0 } });
    vi.mocked(api.artifacts).mockResolvedValue({ items: [], meta: { limit: 500, offset: 0, count: 0 } });
    vi.mocked(api.events).mockResolvedValue({ items: [], after_id: 0 });
  });

  it("confirms retry, reuses checkpoints and refreshes the job navigation state", async () => {
    const user = userEvent.setup();
    vi.mocked(api.job).mockResolvedValueOnce(failedJob).mockResolvedValue(retriedJob);
    vi.mocked(api.retryJob).mockResolvedValue(retriedJob);
    const { queryClient } = renderFailedJob();
    const invalidateQueries = vi.spyOn(queryClient, "invalidateQueries");

    expect(await screen.findByText(/A fejezetlista létrehozása sikertelen volt/)).toBeInTheDocument();
    expect(screen.getByText("A munkafájlok a biztonságos folytatáshoz megmaradtak")).toBeInTheDocument();
    expect(screen.getByText(/A takarítás szándékosan vár/)).toBeInTheDocument();

    await user.click(await screen.findByRole("button", { name: "Folytatás a hibától" }));

    const dialog = screen.getByRole("dialog", { name: "Folytatod a hibától?" });
    expect(within(dialog).getByText("A kész munka nem vész el")).toBeInTheDocument();
    expect(within(dialog).getByText(/érvényes szakasz-checkpointokat/)).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Folytatás a hibától" }));

    await waitFor(() =>
      expect(api.retryJob).toHaveBeenCalledWith("job-1", failedJob.version),
    );
    expect(await screen.findByText("A munka folytatása elindult")).toBeInTheDocument();
    expect(screen.getByText("78%")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Folytatod a hibától?" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Vissza a várólistához/ })).toBeInTheDocument();
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ["job", "job-1"] });
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ["jobs"] });
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ["events", "job-1"] });
  });

  it("keeps the confirmation open and shows the backend error", async () => {
    const user = userEvent.setup();
    vi.mocked(api.retryJob).mockRejectedValue(
      new ApiError(409, "Egy másik munka jelenleg blokkolja a várólistát", {
        detail: "Egy másik munka jelenleg blokkolja a várólistát",
      }),
    );
    renderFailedJob();

    await user.click(await screen.findByRole("button", { name: "Folytatás a hibától" }));
    const dialog = screen.getByRole("dialog", { name: "Folytatod a hibától?" });
    await user.click(within(dialog).getByRole("button", { name: "Folytatás a hibától" }));

    expect(await within(dialog).findByText("A folytatás nem indítható")).toBeInTheDocument();
    expect(within(dialog).getByText("Egy másik munka jelenleg blokkolja a várólistát")).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Folytatod a hibától?" })).toBeInTheDocument();
  });

  it("does not offer checkpoint retry for a non-retryable scan failure", async () => {
    vi.mocked(api.job).mockResolvedValue(
      makeJob({
        state: "FAILED",
        error: "RuntimeError: unreadable disc",
        resume_state: "SCANNING",
      }),
    );
    renderFailedJob();

    expect(await screen.findByRole("link", { name: /Vissza az archívumhoz/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Folytatás a hibától" })).not.toBeInTheDocument();
    expect(screen.queryByText("A munkafájlok a biztonságos folytatáshoz megmaradtak")).not.toBeInTheDocument();
  });

  it("offers a dedicated comparison continuation instead of the selection wizard", async () => {
    const user = userEvent.setup();
    const paused = makeJob({
      state: "NEEDS_REVIEW",
      resume_state: "COMPARISON",
      progress: 0.95,
      status_message: "fast comparison exceeded its bounded command/time budget",
      selection: {
        playlist_id: "00001",
        angle: 1,
        output_name: "Mintafilm.BluRay.x264",
        video: {
          detail_level: "beginner",
          temporal_filter: "progressive",
          crop: { left: 0, top: 0, right: 0, bottom: 0 },
          settings: {},
        },
        tracks: [],
        upload_images: false,
        dual_type_match: false,
      },
    });
    const resumed = makeJob({
      state: "COMPARISON",
      progress: 0.95,
      status_message: "fast comparison: preparing bounded samples",
      selection: paused.selection,
      version: 2,
    });
    vi.mocked(api.job).mockResolvedValue(paused);
    vi.mocked(api.resumeJob).mockResolvedValue(resumed);
    renderFailedJob();

    expect(await screen.findByText("A gyors comparison időkorlátja lejárt")).toBeInTheDocument();
    expect(screen.queryByText("Vizsgáld felül a beállításokat")).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Beállítások" }));
    expect(screen.getByText("A jóváhagyott terv változatlan")).toBeInTheDocument();
    expect(screen.getByText("Kötelező · régi mentés felülbírálva")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Terv ellenőrzése" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Áttekintés" }));
    await user.click(screen.getByRole("button", { name: "Folytatás a comparisontól" }));

    await waitFor(() => expect(api.resumeJob).toHaveBeenCalledWith("job-1"));
    expect(await screen.findByText("A munka folytatása elindult")).toBeInTheDocument();
  });
});

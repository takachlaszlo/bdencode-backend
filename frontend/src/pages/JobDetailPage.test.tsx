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
      pauseJob: vi.fn(),
      continueJob: vi.fn(),
      requestCancelJob: vi.fn(),
      retryJob: vi.fn(),
      restartJob: vi.fn(),
      purgeJob: vi.fn(),
      jobStorage: vi.fn(),
      cleanupJob: vi.fn(),
      deleteJobRelease: vi.fn(),
      releasePreparations: vi.fn(),
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
    vi.mocked(api.jobStorage).mockResolvedValue({ workspace_bytes: 1024, reclaimable_bytes: 512, completed_release_bytes: 0, categories: [] });
    vi.mocked(api.releasePreparations).mockResolvedValue([]);
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

    await user.click(await screen.findByText("Műveletek"));
    await user.click(screen.getByRole("button", { name: "Folytatás a hibától" }));

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

    await user.click(await screen.findByText("Műveletek"));
    await user.click(screen.getByRole("button", { name: "Folytatás a hibától" }));
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

  it("offers restart and destructive workspace deletion for a cancelled job", async () => {
    const user = userEvent.setup();
    const cancelled = makeJob({
      state: "CANCELLED",
      status_message: "cancelled",
      error: null,
      resume_state: null,
      version: 6,
    });
    const restarted = makeJob({
      state: "READY",
      status_message: "cancelled job restored to the configured queue",
      error: null,
      resume_state: null,
      version: 7,
    });
    vi.mocked(api.job).mockResolvedValue(cancelled);
    vi.mocked(api.restartJob).mockResolvedValue(restarted);
    vi.mocked(api.purgeJob).mockResolvedValue(undefined);
    const first = renderFailedJob();

    await user.click(await screen.findByText("Műveletek"));
    await user.click(screen.getByRole("button", { name: "Újraindítás" }));
    const restartDialog = screen.getByRole("dialog", { name: "Újraindítod a megszakított munkát?" });
    expect(within(restartDialog).getByText("A korábbi beállítások megmaradnak")).toBeInTheDocument();
    await user.click(within(restartDialog).getByRole("button", { name: "Újraindítás" }));
    await waitFor(() => expect(api.restartJob).toHaveBeenCalledWith("job-1", 6));
    first.unmount();

    vi.mocked(api.job).mockResolvedValue(cancelled);
    const second = renderFailedJob();
    await user.click(await screen.findByText("Műveletek"));
    await user.click(screen.getByRole("button", { name: "Munka törlése" }));
    const purgeDialog = screen.getByRole("dialog", { name: "Végleg törlöd ezt a munkát?" });
    expect(within(purgeDialog).getByText(/completed release-hez a rendszer nem nyúl/)).toBeInTheDocument();
    await user.type(within(purgeDialog).getByRole("textbox"), cancelled.name);
    await user.click(within(purgeDialog).getByRole("button", { name: "Munka végleges törlése" }));
    await waitFor(() => expect(api.purgeJob).toHaveBeenCalledWith("job-1", 6));
    second.unmount();
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

  it("pins a durable pause request to the current control revision", async () => {
    const user = userEvent.setup();
    const running = {
      ...makeJob({ state: "ENCODING", progress: 0.34 }),
      control_state: "RUNNING" as const,
      control_revision: 4,
      allowed_operations: ["pause", "cancel"],
    };
    const pauseRequested = {
      ...running,
      control_state: "PAUSE_REQUESTED" as const,
      control_revision: 5,
      control_requested_at: "2026-08-16T12:00:00Z",
      allowed_operations: ["cancel"],
    };
    vi.mocked(api.job).mockResolvedValueOnce(running).mockResolvedValue(pauseRequested);
    vi.mocked(api.pauseJob).mockResolvedValue(pauseRequested);
    renderFailedJob();

    await user.click(await screen.findByRole("button", { name: "Szüneteltetés" }));

    await waitFor(() => expect(api.pauseJob).toHaveBeenCalledWith("job-1", 4));
    expect(await screen.findByText("A szüneteltetés kérése rögzítve")).toBeInTheDocument();
  });

  it("keeps completed cleanup and seeded release deletion as separate guarded actions", async () => {
    const user = userEvent.setup();
    const completed = {
      ...makeJob({
        state: "COMPLETED",
        output_path: "/completed/Release.Name/Release.Name.mkv",
        selection: { output_name: "Release.Name" },
        version: 8,
      }),
      allowed_operations: ["cleanup", "delete", "prepare_release", "delete_release"],
    };
    const outputSha = "a".repeat(64);
    vi.mocked(api.job).mockResolvedValue(completed);
    vi.mocked(api.jobStorage).mockResolvedValue({
      workspace_bytes: 4096,
      reclaimable_bytes: 2048,
      completed_release_bytes: 8192,
      release_present: true,
      cleanup_allowed: true,
      categories: [{ name: "work", bytes: 2048, file_count: 3, reclaimable: true, present: true }],
    });
    vi.mocked(api.artifacts).mockResolvedValue({
      items: [{
        id: "output-1",
        job_id: "job-1",
        scan_id: null,
        kind: "OUTPUT",
        name: "Release.Name.mkv",
        path: "/completed/Release.Name/Release.Name.mkv",
        mime_type: "video/x-matroska",
        sha256: outputSha,
        size_bytes: 8192,
        metadata: {},
        created_at: "2026-08-16T10:00:00Z",
      }],
      meta: { limit: 500, offset: 0, count: 1 },
    });
    vi.mocked(api.cleanupJob).mockResolvedValue(undefined);
    vi.mocked(api.deleteJobRelease).mockResolvedValue(undefined);
    vi.mocked(api.releasePreparations).mockResolvedValue([
      { id: "prep-2", job_id: "job-1", state: "READY", version: 12 },
      { id: "prep-1", job_id: "job-1", state: "FAILED", version: 4 },
    ]);
    const { queryClient } = renderFailedJob();

    const storageCard = (await screen.findByRole("heading", { name: "Tárhely és takarítás" })).closest("section");
    if (!storageCard) throw new Error("A tárhelykártya nem található");
    await user.click(within(storageCard).getByRole("button", { name: "Ideiglenes fájlok takarítása" }));
    const cleanupDialog = screen.getByRole("dialog", { name: "Kitakarítod az ideiglenes fájlokat?" });
    await user.click(within(cleanupDialog).getByRole("button", { name: "Takarítás" }));
    await waitFor(() => expect(api.cleanupJob).toHaveBeenCalledWith("job-1", 8));

    await user.click(screen.getByText("Műveletek"));
    await user.click(screen.getByRole("button", { name: "Completed release törlése" }));
    const releaseDialog = screen.getByRole("dialog", { name: "Végleg törlöd a completed release-t?" });
    expect(within(releaseDialog).getByText(outputSha)).toBeInTheDocument();
    expect(within(releaseDialog).getByText('{"prep-1":4,"prep-2":12}')).toBeInTheDocument();
    queryClient.setQueryData(["release-preparations", "job-1"], [
      { id: "prep-1", job_id: "job-1", state: "READY", version: 99 },
    ]);
    await user.type(within(releaseDialog).getByRole("textbox"), "Release.Name");
    await user.click(within(releaseDialog).getByRole("checkbox", { name: /Kényszerített törlés külső vagy seedelt eredmény ellenére/ }));
    await user.click(within(releaseDialog).getByRole("button", { name: "Release végleges törlése" }));

    await waitFor(() => expect(api.deleteJobRelease).toHaveBeenCalledWith("job-1", {
      confirmation: "Release.Name",
      expected_sha256: outputSha,
      force_if_seeded: true,
      preparation_versions: { "prep-1": 4, "prep-2": 12 },
    }));
  });
});

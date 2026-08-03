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
});

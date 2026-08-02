import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SelectionValidation } from "../api/types";
import { api } from "../api/client";
import { makeJob, makeScan } from "../test/fixtures";
import { renderApp } from "../test/render";
import { SelectionWizard } from "./SelectionWizard";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      profileSchema: vi.fn(),
      profileRecommendation: vi.fn(),
      validateSelection: vi.fn(),
      saveSelection: vi.fn(),
    },
  };
});

describe("SelectionWizard", () => {
  const validation: SelectionValidation = {
    valid: true,
    playlist_id: "00001",
    encoder: "x264",
    settings: { crf: 18, preset: "slow", profile: "high" },
    ffmpeg_video_args: ["--crf", "18"],
    crop: { left: 0, top: 0, right: 0, bottom: 0 },
    temporal_filter: "progressive",
    advisory_warnings: [],
  };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.profileSchema).mockResolvedValue({
      encoder: "x264",
      detail_level: "beginner",
      fields: [
        {
          name: "crf",
          group: "rate_control",
          introduced_at: "beginner",
          required: true,
          default: 18,
          value_type: "number",
          minimum: 0,
          maximum: 51,
          choices: [],
          description: "Minőség",
        },
      ],
    });
    vi.mocked(api.profileRecommendation).mockResolvedValue({
      source: "deterministic_expert_rules",
      requires_operator_confirmation: true,
      settings: { crf: 18, preset: "slow", profile: "high" },
    });
    vi.mocked(api.validateSelection).mockResolvedValue(validation);
    vi.mocked(api.saveSelection).mockResolvedValue(
      makeJob({ state: "READY", version: 2 }),
    );
  });

  it("walks through playlist, tracks and video before server validation and save", async () => {
    const user = userEvent.setup();
    const onComplete = vi.fn();
    const job = makeJob({ state: "AWAITING_SELECTION", settings: { detail_level: "beginner" } });

    renderApp(<SelectionWizard job={job} scan={makeScan()} onComplete={onComplete} />);

    expect(screen.getByText("Playlist 00001")).toBeInTheDocument();
    await waitFor(() => expect(api.profileRecommendation).toHaveBeenCalled());

    await user.click(screen.getByRole("button", { name: "Tovább" }));
    expect(screen.getByRole("heading", { name: "Hangsávok" })).toBeInTheDocument();
    expect(screen.getByDisplayValue("eng")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Tovább" }));
    expect(screen.getByRole("heading", { name: "Ajánlott profil" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Tovább" }));
    await user.click(screen.getByRole("button", { name: "Terv ellenőrzése" }));

    expect(await screen.findByText("A terv érvényes")).toBeInTheDocument();
    expect(api.validateSelection).toHaveBeenCalledWith(
      "job-1",
      expect.objectContaining({
        playlist_id: "00001",
        output_name: "Mintafilm.BluRay.x264",
        video: expect.objectContaining({
          detail_level: "beginner",
          settings: expect.objectContaining({ crf: 18 }),
        }),
        tracks: [expect.objectContaining({ stream_id: "audio:4352", action: "copy", language: "eng" })],
      }),
      1,
    );

    await user.click(
      screen.getByRole("button", { name: "Jóváhagyás és várólistára helyezés" }),
    );
    await waitFor(() => expect(api.saveSelection).toHaveBeenCalledTimes(1));
    expect(onComplete).toHaveBeenCalledTimes(1);
  });

  it("opens a malformed NEEDS_REVIEW selection with scan-derived repair defaults", async () => {
    const user = userEvent.setup();
    const job = makeJob({
      state: "NEEDS_REVIEW",
      selection: { unexpected: "legacy-or-corrupt-payload" },
      settings: { detail_level: "beginner" },
    });

    renderApp(<SelectionWizard job={job} scan={makeScan()} onComplete={vi.fn()} />);

    expect(screen.getByText("Playlist 00001")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Tovább" }));
    expect(screen.getByRole("heading", { name: "Hangsávok" })).toBeInTheDocument();
    expect(screen.getByDisplayValue("eng")).toBeInTheDocument();
  });
});

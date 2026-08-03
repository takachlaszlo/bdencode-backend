import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SelectionValidation } from "../api/types";
import { api, ApiError } from "../api/client";
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
    for (const label of ["Copy", "FLAC", "AC-3", "E-AC-3", "DTS", "Kihagyás"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
    await user.click(screen.getByRole("button", { name: "E-AC-3" }));
    expect(screen.getByText(/1024 kb\/s · 48 kHz · legfeljebb 5\.1/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Tovább" }));
    expect(screen.getByRole("heading", { name: "Ajánlott profil" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Tovább" }));
    const strictMatch = screen.getByRole("checkbox", { name: "Szigorú I/P/B típusazonosság kötelező" });
    expect(strictMatch).toBeChecked();
    expect(strictMatch).toBeDisabled();
    await user.selectOptions(screen.getByLabelText("Képtárhely"), "catbox");
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
        tracks: [expect.objectContaining({ stream_id: "audio:4352", action: "eac3", language: "eng" })],
        image_upload_provider: "catbox",
        dual_type_match: true,
      }),
      1,
    );

    await user.click(
      screen.getByRole("button", { name: "Jóváhagyás és automatikus indítás" }),
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

  it("lists missing source color fields and sends the explicitly confirmed BD defaults", async () => {
    const user = userEvent.setup();
    const scan = makeScan();
    const sourceVideo = scan.playlists[0].streams[0].video;
    if (!sourceVideo) throw new Error("A teszt videósávja hiányzik");
    scan.playlists[0].streams[0] = {
      ...scan.playlists[0].streams[0],
      video: {
        ...sourceVideo,
        color_primaries: null,
        color_transfer: null,
        color_matrix: null,
      },
    };
    const job = makeJob({ state: "AWAITING_SELECTION", settings: { detail_level: "beginner" } });

    const view = renderApp(<SelectionWizard job={job} scan={scan} onComplete={vi.fn()} />);
    await user.click(view.getByRole("button", { name: "Tovább" }));
    await user.click(view.getByRole("button", { name: "Tovább" }));

    expect(view.getByRole("heading", { name: "Forrás színinformációjának megerősítése" })).toBeInTheDocument();
    expect(view.getByText("Színprimerek", { selector: ".badge" })).toBeInTheDocument();
    expect(view.getByText("Átviteli karakterisztika", { selector: ".badge" })).toBeInTheDocument();
    expect(view.getByText("Mátrixegyütthatók", { selector: ".badge" })).toBeInTheDocument();
    expect(view.getByText("Ajánlott alapérték: SDR Blu-ray · BT.709")).toBeInTheDocument();

    await user.click(view.getByRole("button", { name: "Ezeknek az értékeknek a jóváhagyása" }));
    expect(view.getByText("Jóváhagyva")).toBeInTheDocument();

    await user.click(view.getByRole("button", { name: "Tovább" }));
    await user.click(view.getByRole("button", { name: "Terv ellenőrzése" }));
    await waitFor(() => expect(api.validateSelection).toHaveBeenCalled());
    expect(api.validateSelection).toHaveBeenCalledWith(
      "job-1",
      expect.objectContaining({
        video: expect.objectContaining({
          settings: expect.objectContaining({
            color: {
              primaries: "bt709",
              transfer: "bt709",
              matrix: "bt709",
              range: "limited",
              chroma_location: "left",
            },
          }),
        }),
      }),
      1,
    );
  });

  it("turns the structured source color API error into an actionable Hungarian message", async () => {
    const user = userEvent.setup();
    vi.mocked(api.validateSelection).mockRejectedValueOnce(new ApiError(
      422,
      "source color metadata is incomplete; confirm it before encoding",
      {
        detail: "source color metadata is incomplete; confirm it before encoding",
        code: "source_color_confirmation_required",
        context: {
          missing_fields: ["primaries", "matrix"],
          suggested: {
            primaries: "bt709",
            transfer: "bt709",
            matrix: "bt709",
            range: "limited",
            chroma_location: "left",
          },
        },
      },
    ));

    const view = renderApp(<SelectionWizard job={makeJob({ state: "AWAITING_SELECTION" })} scan={makeScan()} onComplete={vi.fn()} />);
    await user.click(view.getByRole("button", { name: "Tovább" }));
    await user.click(view.getByRole("button", { name: "Tovább" }));
    await user.click(view.getByRole("button", { name: "Tovább" }));
    await user.click(view.getByRole("button", { name: "Terv ellenőrzése" }));

    expect(await view.findByText("Hiányos forrás-színinformáció")).toBeInTheDocument();
    expect(view.getAllByText(/Színprimerek, Mátrixegyütthatók/)).toHaveLength(2);
    await user.click(view.getByRole("button", { name: "Színadatok megnyitása" }));
    expect(view.getByRole("heading", { name: "Forrás színinformációjának megerősítése" })).toBeInTheDocument();
    expect(view.queryByText(/source color metadata is incomplete/i)).not.toBeInTheDocument();
  });
});

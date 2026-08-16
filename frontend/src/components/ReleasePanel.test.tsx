import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { ReleasePreparation, ReleaseValidationResult } from "../api/types";
import { makeJob } from "../test/fixtures";
import { renderApp } from "../test/render";
import { ReleasePanel } from "./ReleasePanel";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      releaseProfiles: vi.fn(),
      releasePreparations: vi.fn(),
      releasePreparation: vi.fn(),
      createReleasePreparation: vi.fn(),
      releasePreparationAction: vi.fn(),
      validateReleasePreparation: vi.fn(),
      exportReleasePreparation: vi.fn(),
      uploadReleasePreparation: vi.fn(),
      deleteReleasePreparation: vi.fn(),
    },
  };
});

const profile = {
  profile_id: "tracker-hu",
  display_name: "Magyar tracker",
  supports_dupe_check: true,
  supports_publish: true,
  supports_qbittorrent: true,
};

const readyPreparation: ReleasePreparation = {
  id: "prep-1",
  job_id: "job-1",
  profile_id: profile.profile_id,
  state: "READY",
  version: 7,
  metadata: {
    schema_version: 1,
    release_name: "Mintafilm.2024.1080p.BluRay.x264",
  },
  payload_path: "Mintafilm.2024.1080p.BluRay.x264/Mintafilm.2024.1080p.BluRay.x264.mkv",
  payload_size: 4_294_967_296,
  payload_sha256: "a".repeat(64),
  kit_ready: true,
  manifest_sha256: "b".repeat(64),
  torrent_infohash: "c".repeat(40),
  torrent_sha256: "d".repeat(64),
};

const completedJob = makeJob({
  state: "COMPLETED",
  output_path: "/release/Mintafilm.2024.1080p.BluRay.x264.mkv",
  requested_by: "eredeti-operátor",
});

function preflightRegion() {
  const heading = screen.getByRole("heading", { name: "Preflight" });
  const card = heading.closest("section");
  if (!card) throw new Error("A Preflight kártya nem található");
  return within(card);
}

describe("ReleasePanel 2.1 release workflow", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.releaseProfiles).mockResolvedValue({ items: [profile], count: 1 });
    vi.mocked(api.releasePreparations).mockResolvedValue([]);
    vi.mocked(api.releasePreparation).mockResolvedValue(readyPreparation);
    vi.mocked(api.createReleasePreparation).mockResolvedValue(readyPreparation);
    vi.mocked(api.releasePreparationAction).mockResolvedValue(readyPreparation);
    vi.mocked(api.validateReleasePreparation).mockResolvedValue({
      valid: true,
      failures: [],
      payload: {
        path: "release/payload.mkv",
        size: 4_294_967_296,
        sha256: "a".repeat(64),
      },
      screenshots: 6,
      profile_digest: "e".repeat(64),
      manifest_sha256: "b".repeat(64),
    });
    vi.mocked(api.uploadReleasePreparation).mockResolvedValue(readyPreparation);
    vi.mocked(api.deleteReleasePreparation).mockResolvedValue(undefined);
  });

  it("loads profile/list data and sends the exact normalized create metadata body", async () => {
    const user = userEvent.setup();
    renderApp(<ReleasePanel job={completedJob} />);

    const tracker = await screen.findByRole("combobox", { name: "Trackerprofil" });
    expect(api.releaseProfiles).toHaveBeenCalledTimes(1);
    expect(api.releasePreparations).toHaveBeenCalledWith("job-1");
    expect(within(tracker).getByRole("option", { name: "Magyar tracker" })).toBeInTheDocument();

    await user.selectOptions(tracker, profile.profile_id);
    await user.clear(screen.getByRole("spinbutton", { name: "Év" }));
    await user.type(screen.getByRole("spinbutton", { name: "Év" }), "2024");
    expect(screen.getByRole("textbox", { name: /Release-név/ })).toHaveValue("Mintafilm.2024.1080p.BluRay.x264");
    expect(screen.getByRole("textbox", { name: /Release-név/ })).toHaveAttribute("readonly");
    await user.clear(screen.getByRole("textbox", { name: "Cím" }));
    await user.type(screen.getByRole("textbox", { name: "Cím" }), "Film");
    await user.type(screen.getByRole("textbox", { name: "Edition" }), "Director's Cut");
    await user.type(screen.getByRole("textbox", { name: "IMDb ID" }), "tt1234567");
    await user.type(screen.getByRole("spinbutton", { name: "TMDb ID" }), "7654");
    await user.clear(screen.getByRole("textbox", { name: "Kategória" }));
    await user.type(screen.getByRole("textbox", { name: "Kategória" }), "Movie");
    await user.clear(screen.getByRole("textbox", { name: "Forrás" }));
    await user.type(screen.getByRole("textbox", { name: "Forrás" }), "Blu-ray");
    await user.clear(screen.getByRole("textbox", { name: "Felbontás" }));
    await user.type(screen.getByRole("textbox", { name: "Felbontás" }), "1080p");
    await user.clear(screen.getByRole("textbox", { name: "Videokodek" }));
    await user.type(screen.getByRole("textbox", { name: "Videokodek" }), "H.264");
    await user.clear(screen.getByRole("textbox", { name: /^Audiókodekek/ }));
    await user.type(screen.getByRole("textbox", { name: /^Audiókodekek/ }), "DTS-HD MA, AC-3");
    await user.clear(screen.getByRole("textbox", { name: /^Nyelvek/ }));
    await user.type(screen.getByRole("textbox", { name: /^Nyelvek/ }), "hu, en");

    await user.click(screen.getByRole("button", { name: "Új előkészítés létrehozása" }));

    await waitFor(() => expect(api.createReleasePreparation).toHaveBeenCalledWith("job-1", {
      profile_id: "tracker-hu",
      metadata: {
        schema_version: 1,
        release_name: "Mintafilm.2024.1080p.BluRay.x264",
        title: "Film",
        year: 2024,
        edition: "Director's Cut",
        imdb_id: "tt1234567",
        tmdb_id: 7654,
        category: "Movie",
        source_media: "Blu-ray",
        resolution: "1080p",
        video_codec: "H.264",
        audio_codecs: ["DTS-HD MA", "AC-3"],
        languages: ["hu", "en"],
      },
    }));
  });

  it("exposes the valid READY preparation actions and pins mutations to its version", async () => {
    const user = userEvent.setup();
    vi.mocked(api.releasePreparations).mockResolvedValue([readyPreparation]);
    renderApp(<ReleasePanel job={completedJob} />);

    expect(await screen.findByRole("button", { name: "Validálás" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Csomag építése" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Dupe check" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Torrent export" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Seed előkészítése" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Trackerfeltöltés" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Terv törlése" })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "Dupe check" }));
    await waitFor(() => expect(api.releasePreparationAction).toHaveBeenCalledWith(
      "prep-1",
      "dupe-check",
      7,
    ));
  });

  it("keeps the validate response as separate preflight evidence", async () => {
    const user = userEvent.setup();
    vi.mocked(api.releasePreparations).mockResolvedValue([readyPreparation]);
    const validation = {
      valid: true,
      failures: [],
      payload: {
        path: "release/payload.mkv",
        size: 4_294_967_296,
        sha256: "f".repeat(64),
      },
      screenshots: 8,
      profile_digest: "e".repeat(64),
      manifest_sha256: "b".repeat(64),
    } satisfies ReleaseValidationResult;
    vi.mocked(api.validateReleasePreparation).mockResolvedValue(validation);
    renderApp(<ReleasePanel job={completedJob} />);

    await user.click(await screen.findByRole("button", { name: "Validálás" }));
    await waitFor(() => expect(api.validateReleasePreparation).toHaveBeenCalledWith(
      "prep-1",
      7,
    ));

    expect(await preflightRegion().findByText("true")).toBeInTheDocument();
    expect(preflightRegion().getByText(/release\/payload\.mkv/)).toBeInTheDocument();
    expect(preflightRegion().getByText("8")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Validálás" })).toBeEnabled();
  });

  it("submits the exact manifest-pinned upload request after explicit confirmation", async () => {
    const user = userEvent.setup();
    const publishable = {
      ...readyPreparation,
      state: "READY_TO_PUBLISH",
      version: 8,
    } satisfies ReleasePreparation;
    vi.mocked(api.releasePreparations).mockResolvedValue([publishable]);
    vi.mocked(api.releasePreparation).mockResolvedValue(publishable);
    vi.mocked(api.uploadReleasePreparation).mockResolvedValue({
      ...publishable,
      state: "PUBLISHED",
      version: 9,
    });
    const { queryClient } = renderApp(<ReleasePanel job={completedJob} />);

    await user.click(await screen.findByRole("button", { name: "Trackerfeltöltés" }));
    const dialog = screen.getByRole("dialog", { name: "Publikálod a release-t?" });
    const operator = within(dialog).getByRole("textbox", { name: /^Jóváhagyó operátor/ });
    expect(operator).toHaveValue("hitelesített proxy-operátor");
    expect(operator).toHaveAttribute("readonly");
    expect(within(dialog).getByText("b".repeat(64))).toBeInTheDocument();

    queryClient.setQueryData(["release-preparation", "prep-1"], {
      ...publishable,
      state: "UNKNOWN",
      version: 99,
      manifest_sha256: "f".repeat(64),
    } satisfies ReleasePreparation);
    await user.click(within(dialog).getByRole("button", { name: "Trackerfeltöltés indítása" }));

    await waitFor(() => expect(api.uploadReleasePreparation).toHaveBeenCalledWith("prep-1", {
      expected_version: 8,
      manifest_sha256: "b".repeat(64),
    }));
  });

  it.each(["NOT_PREPARED", "NEEDS_REVIEW", "FAILED"] as const)(
    "allows building %s only while no kit exists",
    async (state) => {
      const buildable = { ...readyPreparation, state, kit_ready: false } satisfies ReleasePreparation;
      vi.mocked(api.releasePreparations).mockResolvedValue([buildable]);
      vi.mocked(api.releasePreparation).mockResolvedValue(buildable);

      renderApp(<ReleasePanel job={completedJob} />);

      expect(await screen.findByRole("button", { name: "Csomag építése" })).toBeEnabled();
    },
  );

  it("pins preparation deletion to the modal-opening revision despite polling", async () => {
    const user = userEvent.setup();
    vi.mocked(api.releasePreparations).mockResolvedValue([readyPreparation]);
    const { queryClient } = renderApp(<ReleasePanel job={completedJob} />);

    await user.click(await screen.findByRole("button", { name: "Terv törlése" }));
    const dialog = screen.getByRole("dialog", { name: "Törlöd ezt az előkészítést?" });
    expect(within(dialog).getByText("b".repeat(64))).toBeInTheDocument();

    queryClient.setQueryData(["release-preparation", "prep-1"], {
      ...readyPreparation,
      version: 21,
      manifest_sha256: "f".repeat(64),
    } satisfies ReleasePreparation);
    await user.click(within(dialog).getByRole("button", { name: "Előkészítés törlése" }));

    await waitFor(() => expect(api.deleteReleasePreparation).toHaveBeenCalledWith("prep-1", 7));
  });

  it.each(["UNKNOWN", "PUBLISHED"] as const)("preserves %s evidence from simple deletion", async (state) => {
    const preserved = { ...readyPreparation, state } satisfies ReleasePreparation;
    vi.mocked(api.releasePreparations).mockResolvedValue([preserved]);
    vi.mocked(api.releasePreparation).mockResolvedValue(preserved);

    renderApp(<ReleasePanel job={completedJob} />);

    expect(await screen.findByRole("button", { name: "Terv törlése" })).toBeDisabled();
  });

  it("prevents a second seed after qBittorrent accepted and started rechecking", async () => {
    const seeded = {
      ...readyPreparation,
      qbittorrent_receipt: { outcome: "ADDED_AND_RECHECKING" },
    } satisfies ReleasePreparation;
    vi.mocked(api.releasePreparations).mockResolvedValue([seeded]);
    vi.mocked(api.releasePreparation).mockResolvedValue(seeded);

    renderApp(<ReleasePanel job={completedJob} />);

    expect(await screen.findByRole("button", { name: "Seed előkészítése" })).toBeDisabled();
  });

  it("does not report a rejected qBittorrent result as success", async () => {
    const user = userEvent.setup();
    const rejected = {
      ...readyPreparation,
      qbittorrent_receipt: { outcome: "REJECTED" },
      version: 8,
    } satisfies ReleasePreparation;
    vi.mocked(api.releasePreparations).mockResolvedValue([readyPreparation]);
    vi.mocked(api.releasePreparationAction).mockResolvedValue(rejected);

    renderApp(<ReleasePanel job={completedJob} />);
    await user.click(await screen.findByRole("button", { name: "Seed előkészítése" }));

    expect(await screen.findByText("A release-művelet ellenőrzést kér")).toBeInTheDocument();
    expect(screen.queryByText("A release-művelet elkészült")).not.toBeInTheDocument();
  });

  it("keeps a preparation error visible until the backend clears it", async () => {
    const failed = { ...readyPreparation, state: "FAILED", error: "A csomag hash-ellenőrzése eltért." } satisfies ReleasePreparation;
    vi.mocked(api.releasePreparations).mockResolvedValue([failed]);
    vi.mocked(api.releasePreparation).mockResolvedValue(failed);

    renderApp(<ReleasePanel job={completedJob} />);

    expect(await screen.findByText("Tartós release-hiba")).toBeInTheDocument();
    expect(screen.getByText("A csomag hash-ellenőrzése eltért.")).toBeInTheDocument();
  });

  it("treats the qBittorrent SEEDING lease as active and non-repeatable", async () => {
    const seeding = { ...readyPreparation, state: "SEEDING", version: 9 } satisfies ReleasePreparation;
    vi.mocked(api.releasePreparations).mockResolvedValue([seeding]);
    vi.mocked(api.releasePreparation).mockResolvedValue(seeding);

    renderApp(<ReleasePanel job={completedJob} />);

    expect((await screen.findAllByText("qBittorrent művelet folyamatban")).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByRole("button", { name: "Seed előkészítése" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Terv törlése" })).toBeDisabled();
  });
});

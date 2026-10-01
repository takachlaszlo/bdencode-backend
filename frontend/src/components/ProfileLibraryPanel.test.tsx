import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../api/client";
import type { LibraryProfile } from "../api/types";
import { renderApp } from "../test/render";
import { downloadJson } from "../utils";
import { portableSettings, ProfileLibraryPanel } from "./ProfileLibraryPanel";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      profileLibrary: vi.fn(),
      saveLibraryProfile: vi.fn(),
      deleteLibraryProfile: vi.fn(),
      exportLibraryProfile: vi.fn(),
      exportLibrary: vi.fn(),
      importLibraryProfiles: vi.fn(),
    },
  };
});
vi.mock("../utils", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../utils")>()),
  downloadJson: vi.fn(),
}));

function profile(overrides: Partial<LibraryProfile> = {}): LibraryProfile {
  const base: LibraryProfile = {
    id: "uhd-grain",
    name: "UHD szemcsés",
    description: "Filmszemcsés UHD",
    encoder: "x265",
    detail_level: "advanced",
    settings: { crf: 17.5, preset: "slower" },
    created_at: "2026-05-01T10:00:00+00:00",
    updated_at: "2026-05-01T10:00:00+00:00",
    selection: { detail_level: "advanced", settings: { crf: 17.5, preset: "slower" } },
  };
  return { ...base, ...overrides };
}

function renderPanel(props: Partial<Parameters<typeof ProfileLibraryPanel>[0]> = {}) {
  const onApply = vi.fn();
  const view = renderApp(
    <ProfileLibraryPanel
      encoder="x265"
      detailLevel="advanced"
      settings={{ crf: 19, preset: "slow", color: { primaries: "bt2020" }, hdr10: { enabled: true }, profile: "main10" }}
      autoCrf={null}
      dynamicHdr="discard"
      onApply={onApply}
      {...props}
    />,
  );
  return { ...view, onApply };
}

describe("ProfileLibraryPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.profileLibrary).mockResolvedValue({
      items: [
        profile(),
        profile({ id: "bd-film", name: "BD film", encoder: "x264" }),
        profile({
          id: "auto-hdr",
          name: "Automatikus",
          auto_crf: { enabled: true, target_vmaf: 94 },
          dynamic_hdr: "auto",
          selection: {
            detail_level: "advanced",
            settings: { crf: 18 },
            auto_crf: { enabled: true, target_vmaf: 94 },
            dynamic_hdr: "auto",
          },
        }),
      ],
      count: 3,
    });
  });

  it("shows only profiles of the current encoder and mentions the hidden ones", async () => {
    renderPanel();

    expect(await screen.findByText("UHD szemcsés")).toBeInTheDocument();
    expect(screen.getByText("Automatikus")).toBeInTheDocument();
    expect(screen.queryByText("BD film")).not.toBeInTheDocument();
    expect(screen.getByText("1 másik kodekhez tartozó profil rejtve.")).toBeInTheDocument();
    expect(screen.getByText("VMAF 94")).toBeInTheDocument();
    expect(screen.getByText("auto")).toBeInTheDocument();
  });

  it("hands the profile's selection fragment to the editor", async () => {
    const user = userEvent.setup();
    const { onApply } = renderPanel();

    await user.click(await screen.findByRole("button", { name: "Automatikus alkalmazása" }));

    expect(onApply).toHaveBeenCalledWith(
      {
        detail_level: "advanced",
        settings: { crf: 18 },
        auto_crf: { enabled: true, target_vmaf: 94 },
        dynamic_hdr: "auto",
      },
      expect.objectContaining({ id: "auto-hdr" }),
    );
    expect(screen.getByText(/bekerült a szerkeszthető mezőkbe/)).toBeInTheDocument();
  });

  it("saves only portable settings plus the automatic CRF and HDR policy", async () => {
    const user = userEvent.setup();
    vi.mocked(api.saveLibraryProfile).mockResolvedValue(profile({ id: "uj", name: "Új" }));
    renderPanel({ autoCrf: { enabled: true, target_vmaf: 95 }, dynamicHdr: "hdr10plus" });

    await user.type(await screen.findByLabelText("Profil neve"), "  Új profil ");
    await user.type(screen.getByLabelText("Profil leírása"), "Mire jó");
    await user.click(screen.getByRole("button", { name: "Mentés" }));

    await waitFor(() => expect(api.saveLibraryProfile).toHaveBeenCalledTimes(1));
    expect(api.saveLibraryProfile).toHaveBeenCalledWith(
      {
        name: "Új profil",
        description: "Mire jó",
        encoder: "x265",
        detail_level: "advanced",
        settings: { crf: 19, preset: "slow" },
        auto_crf: { enabled: true, target_vmaf: 95 },
        dynamic_hdr: "hdr10plus",
      },
      false,
    );
    expect(await screen.findByText("A profil elmentve.")).toBeInTheDocument();
  });

  it("omits the optional blocks when they are off and refuses an empty name", async () => {
    const user = userEvent.setup();
    vi.mocked(api.saveLibraryProfile).mockResolvedValue(profile());
    renderPanel();

    const save = await screen.findByRole("button", { name: "Mentés" });
    expect(save).toBeDisabled();
    await user.type(screen.getByLabelText("Profil neve"), "Egyszerű");
    await user.click(save);
    await waitFor(() => expect(api.saveLibraryProfile).toHaveBeenCalled());
    const saved = vi.mocked(api.saveLibraryProfile).mock.calls[0][0];
    expect(saved).not.toHaveProperty("auto_crf");
    expect(saved).not.toHaveProperty("dynamic_hdr");
  });

  it("offers an overwrite when the name already exists", async () => {
    const user = userEvent.setup();
    vi.mocked(api.saveLibraryProfile)
      .mockRejectedValueOnce(new ApiError(409, "a profile with id 'egyszeru' already exists", null))
      .mockResolvedValueOnce(profile());
    renderPanel();

    await user.type(await screen.findByLabelText("Profil neve"), "Egyszerű");
    await user.click(screen.getByRole("button", { name: "Mentés" }));
    await user.click(await screen.findByRole("button", { name: "Felülírás" }));

    await waitFor(() => expect(api.saveLibraryProfile).toHaveBeenLastCalledWith(expect.anything(), true));
  });

  it("imports a file with the chosen conflict mode and reports per-entry results", async () => {
    const user = userEvent.setup();
    vi.mocked(api.importLibraryProfiles).mockResolvedValue({
      imported: [{ name: "Jó", id: "jo" }],
      skipped: [],
      errors: [{ name: "Rossz", code: "invalid_settings", message: "invalid settings: CRF must be finite" }],
    });
    renderPanel();

    await user.selectOptions(await screen.findByLabelText("Név ütközése importnál"), "skip");
    const document = { format: "bdencode-profile", name: "Jó", encoder: "x265", settings: {} };
    await user.upload(
      screen.getByTestId("profile-import-input"),
      new File([JSON.stringify(document)], "profil.json", { type: "application/json" }),
    );

    await waitFor(() => expect(api.importLibraryProfiles).toHaveBeenCalledWith(document, "skip"));
    expect(await screen.findByText(/1 importálva, 0 kihagyva, 1 hibás/)).toBeInTheDocument();
    expect(screen.getByText(/Rossz: invalid settings: CRF must be finite/)).toBeInTheDocument();
  });

  it("rejects a file that is not JSON or not an object before calling the server", async () => {
    const user = userEvent.setup();
    renderPanel();
    const input = await screen.findByTestId("profile-import-input");

    await user.upload(input, new File(["{nem json"], "rossz.json", { type: "application/json" }));
    expect(await screen.findByText("A fájl nem érvényes JSON.")).toBeInTheDocument();
    await user.upload(input, new File(["[1,2]"], "tomb.json", { type: "application/json" }));
    expect(await screen.findByText("A fájl nem BDEncode profil vagy profilcsomag.")).toBeInTheDocument();
    expect(api.importLibraryProfiles).not.toHaveBeenCalled();
  });

  it("exports one profile and the whole library as JSON downloads", async () => {
    const user = userEvent.setup();
    vi.mocked(api.exportLibraryProfile).mockResolvedValue({ format: "bdencode-profile", name: "UHD szemcsés" });
    vi.mocked(api.exportLibrary).mockResolvedValue({ format: "bdencode-profile-bundle", profiles: [] });
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "UHD szemcsés exportálása" }));
    await waitFor(() =>
      expect(downloadJson).toHaveBeenCalledWith("uhd-grain.bdencode-profile.json", { format: "bdencode-profile", name: "UHD szemcsés" }),
    );
    await user.click(screen.getByRole("button", { name: "Teljes export" }));
    await waitFor(() => expect(downloadJson).toHaveBeenCalledWith("bdencode-profiles.json", expect.objectContaining({ format: "bdencode-profile-bundle" })));
  });

  it("asks for confirmation before deleting a profile", async () => {
    const user = userEvent.setup();
    vi.mocked(api.deleteLibraryProfile).mockResolvedValue(undefined);
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "UHD szemcsés törlése" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/végleg törlődik/)).toBeInTheDocument();
    expect(api.deleteLibraryProfile).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Törlés" }));
    await waitFor(() => expect(api.deleteLibraryProfile).toHaveBeenCalledWith("uhd-grain"));
  });

  it("degrades gracefully when the library cannot be read", async () => {
    vi.mocked(api.profileLibrary).mockRejectedValue(new ApiError(422, "the profile library is not configured", null));
    renderPanel();
    expect(await screen.findByText(/A profilkönyvtár nem érhető el: the profile library is not configured/)).toBeInTheDocument();
  });
});

describe("portableSettings", () => {
  it("drops every disc-specific and bitstream-policy field", () => {
    expect(
      portableSettings({
        crf: 18,
        preset: "slow",
        noise_reduction: 100,
        encoder: "x265",
        detail_level: "pro",
        profile: "main10",
        level: "5.1",
        bit_depth: 10,
        pixel_format: "yuv420p10le",
        color: {},
        vbv: {},
        hdr10: {},
        aud: true,
        repeat_headers: true,
        annexb: true,
      }),
    ).toEqual({ crf: 18, preset: "slow", noise_reduction: 100 });
  });
});

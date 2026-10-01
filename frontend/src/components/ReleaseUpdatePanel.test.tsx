import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { renderApp } from "../test/render";
import { ReleaseUpdatePanel } from "./ReleaseUpdatePanel";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { ...actual.api, releaseUpdate: vi.fn() } };
});

describe("ReleaseUpdatePanel", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows an installed update with versions and times", async () => {
    vi.mocked(api.releaseUpdate).mockResolvedValue({
      available: true,
      status: {
        state: "installed", message: "BDEncode 2.2.0 was updated to v2.2.1", checked_at: "2026-10-02T00:30:00+00:00",
        installed_at: "2026-10-02T00:31:00+00:00", installed_version: "2.2.1", latest_tag: "v2.2.1",
      },
    });
    renderApp(<ReleaseUpdatePanel />);
    expect(await screen.findByText("Frissítve")).toHaveClass("badge--success");
    expect(screen.getByText("2.2.1")).toBeInTheDocument();
    expect(screen.getByText("v2.2.1")).toBeInTheDocument();
    expect(screen.getByText("BDEncode 2.2.0 was updated to v2.2.1")).toBeInTheDocument();
    expect(screen.getByText(/bdencode-release-update install --tag/)).toBeInTheDocument();
  });

  it("warns about a failed installation and pending media updates", async () => {
    vi.mocked(api.releaseUpdate).mockResolvedValue({
      available: true,
      status: {
        state: "install_failed", message: "installing v2.3.0 failed", checked_at: "2026-10-02T00:30:00+00:00",
        media_updates: ["ffmpeg 7.1.5 -> 7.1.6"],
      },
    });
    renderApp(<ReleaseUpdatePanel />);
    expect(await screen.findByText("A telepítés nem sikerült")).toHaveClass("badge--danger");
    expect(screen.getByText("ffmpeg 7.1.5 -> 7.1.6")).toBeInTheDocument();
    expect(screen.getByText(/nem települ automatikusan/)).toBeInTheDocument();
  });

  it("explains an empty status instead of failing", async () => {
    vi.mocked(api.releaseUpdate).mockResolvedValue({ available: false, status: null });
    renderApp(<ReleaseUpdatePanel />);
    expect(await screen.findByText(/Még nincs adat/)).toBeInTheDocument();
  });
});

import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { makeJob } from "../test/fixtures";
import { renderApp } from "../test/render";
import { JobsPage } from "./JobsPage";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      jobs: vi.fn(),
    },
  };
});

describe("JobsPage queue automation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.jobs).mockResolvedValue({
      items: [makeJob({ state: "READY", status_message: "Referencia előkészítése" })],
      meta: { limit: 500, offset: 0, count: 1 },
    });
  });

  it("explains that the first job starts automatically and labels READY as preparation", async () => {
    renderApp(<JobsPage mode="queue" />, "/queue");

    expect(await screen.findByText("Előkészítés")).toBeInTheDocument();
    expect(
      screen.getByText(/külön indítógomb nélkül, automatikusan dolgozza fel/),
    ).toBeInTheDocument();
  });
});

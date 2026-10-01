import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { JobStatistics, StatisticsResponse } from "../api/types";
import { renderApp } from "../test/render";
import { StatisticsPage, statisticsCsv } from "./StatisticsPage";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { ...actual.api, statistics: vi.fn() } };
});

const GIB = 1024 ** 3;

function row(overrides: Partial<JobStatistics> = {}): JobStatistics {
  return {
    job_id: "job-1",
    name: "Alfa film",
    state: "COMPLETED",
    disc_type: "UHD",
    content_type: "FILM",
    finished_at: "2026-06-01T12:00:00Z",
    encoder: "x265",
    crf: 17.75,
    preset: "slow",
    source_bytes: 40 * GIB,
    output_bytes: 10 * GIB,
    saved_bytes: 30 * GIB,
    saved_percent: 75,
    media_seconds: 7200,
    frames: 172627,
    bitrate_kbps: 11650,
    encode_seconds: 3300,
    encode_fps: 52.31,
    realtime_factor: 2.18,
    total_seconds: 18000,
    quality: { vmaf_sample: 95.2, vmaf_target: 95, vmaf_scope: "auto-crf sample encodes", ssim_mean: 0.98712, psnr_mean_db: 44.57, comparison_samples: 24 },
    auto_crf: { status: "converged", chosen_crf: 17.75, probes: 3 },
    ...overrides,
  };
}

function response(jobs: JobStatistics[]): StatisticsResponse {
  return {
    summary: {
      jobs: jobs.length,
      jobs_with_size_evidence: jobs.length,
      source_gib: 60,
      output_gib: 25,
      saved_gib: 35,
      saved_percent: 58.33,
      total_output_gib: 25,
      average_vmaf_sample: 95.2,
      average_ssim: 0.9871,
      average_psnr_db: 44.57,
      average_crf: 18.5,
      average_bitrate_kbps: 9000,
      encode_hours: 1.5,
      average_encode_fps: 52.31,
      average_realtime_factor: 2.18,
      encoders: { x265: 1, x264: 1 },
    },
    jobs,
  };
}

describe("StatisticsPage", () => {
  beforeEach(() => vi.clearAllMocks());

  it("summarizes savings, quality and speed and lists every file", async () => {
    vi.mocked(api.statistics).mockResolvedValue(
      response([
        row(),
        row({ job_id: "job-2", name: "Béta film", encoder: "x264", crf: 18, auto_crf: null, finished_at: "2026-01-01T12:00:00Z", saved_percent: 25, saved_bytes: 5 * GIB, quality: { vmaf_sample: null, vmaf_target: null, vmaf_scope: null, ssim_mean: 0.96, psnr_mean_db: null, comparison_samples: 24 } }),
      ]),
    );
    renderApp(<StatisticsPage />, "/statistics");

    const summary = await screen.findByLabelText("Összesítés");
    expect(within(summary).getByText("35.0 GiB")).toBeInTheDocument();
    expect(within(summary).getByText(/58\.3%/)).toBeInTheDocument();
    expect(within(summary).getByText("95.20")).toBeInTheDocument();
    expect(within(summary).getByText("52.31 fps")).toBeInTheDocument();
    expect(within(summary).getByText(/x265: 1 · x264: 1/)).toBeInTheDocument();

    const table = screen.getByRole("table");
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(3);
    expect(within(rows[1]).getByRole("link", { name: "Alfa film" })).toHaveAttribute("href", "/jobs/job-1");
    expect(within(rows[1]).getByText("auto")).toBeInTheDocument();
    expect(within(rows[1]).getByText(/40\.0 GiB → 10\.0 GiB/)).toBeInTheDocument();
    expect(within(rows[2]).getAllByText(/—/).length).toBeGreaterThan(0);
  });

  it("sorts by the chosen column and flips the direction", async () => {
    const user = userEvent.setup();
    vi.mocked(api.statistics).mockResolvedValue(
      response([
        row({ job_id: "a", name: "Alfa", saved_percent: 10 }),
        row({ job_id: "b", name: "Béta", saved_percent: 60 }),
        row({ job_id: "c", name: "Gamma", saved_percent: null, saved_bytes: null }),
      ]),
    );
    renderApp(<StatisticsPage />, "/statistics");
    await screen.findByRole("table");

    const order = () => within(screen.getByRole("table")).getAllByRole("row").slice(1).map((item) => within(item).getByRole("link").textContent);
    await user.selectOptions(screen.getByLabelText("Rendezés"), "saved_percent");
    expect(order()).toEqual(["Béta", "Alfa", "Gamma"]);
    await user.click(screen.getByRole("button", { name: "Csökkenő" }));
    expect(order()).toEqual(["Gamma", "Alfa", "Béta"]);
    await user.selectOptions(screen.getByLabelText("Rendezés"), "name");
    expect(order()).toEqual(["Alfa", "Béta", "Gamma"]);
  });

  it("shows an empty state, an error and never invents numbers for missing evidence", async () => {
    vi.mocked(api.statistics).mockResolvedValueOnce(response([]));
    const first = renderApp(<StatisticsPage />, "/statistics");
    expect(await screen.findByText("Még nincs elkészült munka")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "CSV letöltése" })).toBeDisabled();
    first.unmount();

    vi.mocked(api.statistics).mockRejectedValueOnce(new Error("hálózati hiba"));
    const second = renderApp(<StatisticsPage />, "/statistics");
    expect(await screen.findByText("A statisztika nem tölthető be")).toBeInTheDocument();
    expect(screen.getByText("hálózati hiba")).toBeInTheDocument();
    second.unmount();

    vi.mocked(api.statistics).mockResolvedValueOnce(
      response([row({ source_bytes: null, saved_bytes: null, saved_percent: null, encode_fps: null, realtime_factor: null, encode_seconds: null, total_seconds: null })]),
    );
    renderApp(<StatisticsPage />, "/statistics");
    const cells = within((await screen.findAllByRole("row"))[1]).getAllByRole("cell");
    expect(cells[2]).toHaveTextContent("— → 10.0 GiB");
    expect(cells[3]).toHaveTextContent("—");
    expect(cells[6]).toHaveTextContent("—");
  });
});

describe("statisticsCsv", () => {
  it("starts with a BOM, quotes special characters and leaves gaps empty", () => {
    const csv = statisticsCsv([
      row({ name: 'Film "A", második; rész', source_bytes: null, saved_bytes: null, saved_percent: null }),
    ]);
    expect(csv.startsWith("﻿nev,kodolo,crf,preset,")).toBe(true);
    const lines = csv.trim().split("\r\n");
    expect(lines).toHaveLength(2);
    expect(lines[1]).toContain('"Film ""A"", második; rész"');
    expect(lines[1]).toContain(",x265,17.75,slow,,10737418240,,,95.2,0.98712,44.57,52.31,3300,2026-06-01T12:00:00Z");
  });
});

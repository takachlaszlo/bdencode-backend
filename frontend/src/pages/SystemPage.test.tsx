import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { CapabilitiesResponse, RuntimeCapabilitiesResponse } from "../api/types";
import { renderApp } from "../test/render";
import { SystemPage } from "./SystemPage";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      health: vi.fn(),
      runtimeCapabilities: vi.fn(),
      capabilities: vi.fn(),
    },
  };
});

const capabilities = {
  api_version: "1",
  backend_version: "2.0.0",
  constraints: { cpu_budget_fraction: 0.8 },
} as CapabilitiesResponse;

function mockRuntime(runtime: RuntimeCapabilitiesResponse) {
  vi.mocked(api.health).mockResolvedValue({
    status: "ok",
    database: "/home/encoder/encode/state/encoder.sqlite3",
    schema_version: 7,
    active_job_id: null,
    blocking_state: null,
    queued_jobs: 0,
  });
  vi.mocked(api.capabilities).mockResolvedValue(capabilities);
  vi.mocked(api.runtimeCapabilities).mockResolvedValue(runtime);
}

describe("SystemPage runtime capabilities", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders omitted doctor fields as unknown instead of failures or zero-byte storage", async () => {
    mockRuntime({
      host: { logical_cpus: 16 },
      tools: {
        vspipe: {
          path: "/home/encoder/encode/app/tools/current/bin/vspipe",
          version: "VapourSynth Video Processing Library",
          available: true,
        },
      },
      ffmpeg: { encoders: ["libx264", "libx265", "flac"] },
    });

    renderApp(<SystemPage />, "/settings");

    const vapourSynth = await screen.findByText("VapourSynth Ismeretlen");
    expect(vapourSynth).toHaveClass("badge--neutral");
    expect(screen.queryByText("VapourSynth hiba")).not.toBeInTheDocument();
    expect(screen.getByText("Tárhelyadatok: Ismeretlen")).toBeInTheDocument();
    expect(screen.queryByText(/0 B/)).not.toBeInTheDocument();
    expect(screen.getByText("Útvonal").nextElementSibling).toHaveTextContent("Ismeretlen");
    expect(screen.getByText("Olvasható").nextElementSibling).toHaveTextContent("Ismeretlen");
    expect(screen.getByText("Írható").nextElementSibling).toHaveTextContent("Ismeretlen");
  });

  it("renders the path, VapourSynth result, warnings and storage values from a full doctor report", async () => {
    mockRuntime({
      status: "ok",
      database: {
        path: "/home/encoder/encode/state/encoder.sqlite3",
        schema_version: 7,
        active_job: null,
      },
      paths: {
        data: {
          path: "/home/encoder/encode",
          exists: true,
          readable: true,
          writable: true,
          free_bytes: 2 * 1024 ** 3,
          total_bytes: 8 * 1024 ** 3,
          ok: true,
        },
        sources: [{ path: "/storage", exists: true, readable: true, writable: false, ok: true }],
      },
      host: { logical_cpus: 16 },
      tools: {
        vspipe: {
          path: "/home/encoder/encode/app/tools/current/bin/vspipe",
          version: "VapourSynth Video Processing Library",
          available: true,
        },
      },
      ffmpeg: { encoders: ["libx264", "libx265", "flac"], filters: [], protocols: ["bluray"] },
      missing_ffmpeg_capabilities: { encoders: [], filters: [], protocols: [] },
      vapoursynth: {
        ok: true,
        plugins: { bs: true, bwdif: true, vivtc: true, resize: true },
        error: null,
      },
      imgbb_credential: { configured: true, encrypted_at_rest: true, permissions: "0600", permissions_ok: true },
      worker_cpu_policy: { requested_percent: 75, logical_cpus: 16, systemd_cpu_quota_percent: 1200 },
      warnings: ["Teszt figyelmeztetés"],
    });

    renderApp(<SystemPage />, "/settings");

    const vapourSynth = await screen.findByText("VapourSynth OK");
    expect(vapourSynth).toHaveClass("badge--success");
    expect(screen.getAllByText("/home/encoder/encode")).toHaveLength(2);
    expect(screen.getByText("2.00 GiB szabad")).toBeInTheDocument();
    expect(screen.getByText("6.00 GiB használatban · 8.00 GiB összesen")).toBeInTheDocument();
    expect(screen.getByText("Olvasható").nextElementSibling).toHaveTextContent("Igen");
    expect(screen.getByText("Írható").nextElementSibling).toHaveTextContent("Igen");
    expect(screen.getByText("75% teljes keret")).toBeInTheDocument();
    expect(screen.getByText("Teszt figyelmeztetés")).toBeInTheDocument();
  });

  it("keeps explicit runtime failures visible", async () => {
    mockRuntime({
      paths: {
        data: {
          path: "/home/encoder/encode",
          exists: true,
          readable: false,
          writable: false,
          free_bytes: 1024,
          total_bytes: 4096,
          ok: false,
        },
      },
      vapoursynth: { ok: false, plugins: {}, error: "plugin import failed" },
    });

    renderApp(<SystemPage />, "/settings");

    const vapourSynth = await screen.findByText("VapourSynth hiba");
    expect(vapourSynth).toHaveClass("badge--danger");
    expect(screen.getByText("Olvasható").nextElementSibling).toHaveTextContent("Nem");
    expect(screen.getByText("Írható").nextElementSibling).toHaveTextContent("Nem");
  });

  it("shows whether the optional AI adviser credential is ready", async () => {
    mockRuntime({
      ai_recommendation: {
        provider: "openai",
        model: "gpt-5.6-terra",
        credential: {
          configured: true,
          ready_for_consumer: true,
          consumer_service: "bdencode-api.service",
        },
      },
    });

    renderApp(<SystemPage />, "/settings");

    expect(await screen.findByText("Scan-alapú profiljavaslat")).toBeInTheDocument();
    expect(screen.getByText("gpt-5.6-terra")).toBeInTheDocument();
    expect(screen.getByText("Használatra kész")).toBeInTheDocument();
  });

  it("shows worker credential readiness instead of the API process runtime view", async () => {
    mockRuntime({
      image_upload_credentials: {
        imgbb: {
          configured: true,
          runtime_loaded: null,
          consumer_service: "bdencode-worker.service",
          service_bound: true,
          service_active: true,
          ready_for_consumer: true,
        },
        catbox: {
          configured: true,
          runtime_loaded: null,
          consumer_service: "bdencode-worker.service",
          service_bound: true,
          service_active: false,
          ready_for_consumer: true,
        },
        freeimage: {
          configured: true,
          runtime_loaded: null,
          consumer_service: "bdencode-worker.service",
          service_bound: false,
          service_active: true,
          ready_for_consumer: false,
        },
      },
    });

    renderApp(<SystemPage />, "/settings");

    expect(await screen.findByText("Használatra kész")).toBeInTheDocument();
    expect(screen.getByText("Bekötve, a worker áll")).toBeInTheDocument();
    expect(screen.getByText("Nincs a workerhez kötve")).toBeInTheDocument();
    expect(screen.getAllByText("bdencode-worker.service")).toHaveLength(3);
  });
});

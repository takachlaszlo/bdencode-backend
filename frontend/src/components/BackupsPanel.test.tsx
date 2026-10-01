import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../api/client";
import type { BackupInfo, DatabaseStatus } from "../api/types";
import { renderApp } from "../test/render";
import { BackupsPanel } from "./BackupsPanel";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: { ...actual.api, databaseStatus: vi.fn(), backups: vi.fn(), createBackup: vi.fn() },
  };
});

const backup = (overrides: Partial<BackupInfo> = {}): BackupInfo => ({
  name: "encoder-20260501T030000Z-scheduled-abcdef01.sqlite3",
  label: "scheduled",
  created_at: "2026-05-01T03:00:00+00:00",
  size_bytes: 2_097_152,
  sha256: "a".repeat(64),
  schema_version: 2,
  jobs: 14,
  verified: false,
  ...overrides,
});

const status = (overrides: Partial<DatabaseStatus> = {}): DatabaseStatus => ({
  path: "/home/enc/encode/state/encoder.sqlite3",
  schema_version: 2,
  size_bytes: 5_242_880,
  integrity: ["ok"],
  migrations: [
    { id: 2, from_version: 1, to_version: 2, kind: "migrate", applied_at: "2026-04-01T10:00:00+00:00", backup_name: "encoder-pre.sqlite3", app_version: "2.2.0" },
    { id: 1, from_version: null, to_version: 1, kind: "create", applied_at: "2026-01-01T10:00:00+00:00", backup_name: null, app_version: "2.0.0" },
  ],
  backup_count: 2,
  latest_backup: backup(),
  ...overrides,
});

describe("BackupsPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.databaseStatus).mockResolvedValue(status());
    vi.mocked(api.backups).mockResolvedValue({
      directory: "/home/enc/encode/state/backups",
      items: [backup(), backup({ name: "encoder-20260401T100000Z-pre-migration-v1-11111111.sqlite3", label: "pre-migration-v1", schema_version: 1, jobs: 3 })],
    });
  });

  it("shows the database state, backups with readable kinds and the schema history", async () => {
    renderApp(<BackupsPanel />);

    await screen.findByRole("table");
    expect(screen.getAllByText("Séma")[0].nextElementSibling).toHaveTextContent("v2");
    expect(screen.getByText("rendben")).toHaveClass("badge--success");
    expect(screen.getByText("5.00 MiB")).toBeInTheDocument();
    const rows = within(screen.getByRole("table")).getAllByRole("row");
    expect(rows).toHaveLength(3);
    expect(within(rows[1]).getByText("Ütemezett")).toBeInTheDocument();
    expect(within(rows[2]).getByText("Migráció előtti")).toBeInTheDocument();
    expect(within(rows[2]).getByText("v1")).toBeInTheDocument();
    expect(screen.getByText(/migráció v1 → v2 · mentés: encoder-pre\.sqlite3 · BDEncode 2\.2\.0/)).toBeInTheDocument();
    expect(screen.getByText(/létrehozva v1/)).toBeInTheDocument();
    expect(screen.getByText(/bdencode db-restore/)).toBeInTheDocument();
  });

  it("flags a failed integrity check", async () => {
    vi.mocked(api.databaseStatus).mockResolvedValue(status({ integrity: ["row 3 missing from index x"] }));
    renderApp(<BackupsPanel />);
    const badge = await screen.findByText("row 3 missing from index x");
    expect(badge).toHaveClass("badge--danger");
  });

  it("creates a backup on demand and refreshes the list", async () => {
    const user = userEvent.setup();
    vi.mocked(api.createBackup).mockResolvedValue(backup({ name: "encoder-new-manual.sqlite3", label: "manual", verified: true }));
    renderApp(<BackupsPanel />);

    await user.click(await screen.findByRole("button", { name: "Mentés most" }));

    await waitFor(() => expect(api.createBackup).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/A mentés elkészült és ellenőrzött: encoder-new-manual\.sqlite3/)).toBeInTheDocument();
    await waitFor(() => expect(vi.mocked(api.backups).mock.calls.length).toBeGreaterThan(1));
  });

  it("reports a failed backup and an empty backup list", async () => {
    const user = userEvent.setup();
    vi.mocked(api.backups).mockResolvedValue({ directory: "/x", items: [] });
    vi.mocked(api.createBackup).mockRejectedValue(new ApiError(500, "database backup failed: disk full", null));
    renderApp(<BackupsPanel />);

    expect(await screen.findByText(/Még nincs mentés/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Mentés most" }));
    expect(await screen.findByText("A mentés nem sikerült")).toBeInTheDocument();
    expect(screen.getByText("database backup failed: disk full")).toBeInTheDocument();
  });

  it("copes with an unreadable status endpoint", async () => {
    vi.mocked(api.databaseStatus).mockRejectedValue(new Error("nem elérhető"));
    renderApp(<BackupsPanel />);
    expect(await screen.findByText("Az adatbázis állapota nem olvasható.")).toBeInTheDocument();
  });
});

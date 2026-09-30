import { useQuery } from "@tanstack/react-query";
import { BarChart3, Download } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router";
import { api } from "../api/client";
import type { JobStatistics } from "../api/types";
import { Badge, Button, Card, EmptyState, LoadingPanel, Notice, PageHeader } from "../components/ui";
import { formatBytes, formatDate, formatDuration, formatGiB, formatNumber, formatPercent } from "../utils";

type SortKey = "finished_at" | "name" | "saved_percent" | "saved_bytes" | "vmaf" | "fps";

const SORT_LABELS: Record<SortKey, string> = {
  finished_at: "Legutóbbi elöl",
  name: "Név",
  saved_percent: "Megtakarítás (%)",
  saved_bytes: "Megtakarítás (GiB)",
  vmaf: "VMAF",
  fps: "Kódolási sebesség",
};

function sortValue(row: JobStatistics, key: SortKey): number | string {
  switch (key) {
    case "name": return row.name.toLowerCase();
    case "saved_percent": return row.saved_percent ?? Number.NEGATIVE_INFINITY;
    case "saved_bytes": return row.saved_bytes ?? Number.NEGATIVE_INFINITY;
    case "vmaf": return row.quality.vmaf_sample ?? Number.NEGATIVE_INFINITY;
    case "fps": return row.encode_fps ?? Number.NEGATIVE_INFINITY;
    default: return row.finished_at ?? "";
  }
}

/** CSV with a UTF-8 BOM so spreadsheet programs open the accents correctly. */
export function statisticsCsv(rows: JobStatistics[]): string {
  const header = [
    "nev", "kodolo", "crf", "preset", "forras_bajt", "kimenet_bajt", "megtakaritas_bajt",
    "megtakaritas_szazalek", "vmaf_minta", "ssim", "psnr_db", "kodolasi_fps", "kodolas_masodperc", "befejezve",
  ];
  const escape = (value: unknown) => {
    const text = value == null ? "" : String(value);
    return /[",\n;]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
  };
  const lines = rows.map((row) => [
    row.name, row.encoder, row.crf, row.preset, row.source_bytes, row.output_bytes, row.saved_bytes,
    row.saved_percent, row.quality.vmaf_sample, row.quality.ssim_mean, row.quality.psnr_mean_db,
    row.encode_fps, row.encode_seconds, row.finished_at,
  ].map(escape).join(","));
  return `﻿${[header.join(","), ...lines].join("\r\n")}\r\n`;
}

export function StatisticsPage() {
  const query = useQuery({ queryKey: ["statistics"], queryFn: () => api.statistics(500), retry: false });
  const [sortKey, setSortKey] = useState<SortKey>("finished_at");
  const [descending, setDescending] = useState(true);

  const rows = useMemo(() => {
    const items = [...(query.data?.jobs ?? [])];
    items.sort((left, right) => {
      const a = sortValue(left, sortKey);
      const b = sortValue(right, sortKey);
      const order = a < b ? -1 : a > b ? 1 : 0;
      return descending ? -order : order;
    });
    return items;
  }, [query.data, sortKey, descending]);

  function downloadCsv() {
    const blob = new Blob([statisticsCsv(rows)], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "bdencode-statisztika.csv";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  const summary = query.data?.summary;
  return (
    <div className="page">
      <PageHeader
        eyebrow="Statisztika"
        title="Elkészült kódolások"
        description="Helymegtakarítás, minőség és kódolási sebesség fájlonként és összesítve."
        actions={<Button variant="secondary" icon={<Download size={17} />} onClick={downloadCsv} disabled={rows.length === 0}>CSV letöltése</Button>}
      />

      {query.isLoading ? <LoadingPanel label="Statisztika betöltése…" /> : query.isError || !summary ? (
        <Notice tone="danger" title="A statisztika nem tölthető be">{query.error instanceof Error ? query.error.message : "Ismeretlen hiba"}</Notice>
      ) : summary.jobs === 0 ? (
        <EmptyState icon={<BarChart3 size={30} />} title="Még nincs elkészült munka" description="A statisztika az első befejezett kódolás után jelenik meg." />
      ) : (
        <>
          <div className="stat-grid" aria-label="Összesítés">
            <Card className="stat-card">
              <span className="eyebrow">Megtakarított hely</span>
              <strong>{formatGiB(summary.saved_gib)}</strong>
              <small>{formatPercent(summary.saved_percent)} · {summary.jobs_with_size_evidence} mérhető munka · forrás {formatGiB(summary.source_gib)} → kimenet {formatGiB(summary.output_gib)}</small>
            </Card>
            <Card className="stat-card">
              <span className="eyebrow">Átlagos VMAF</span>
              <strong>{formatNumber(summary.average_vmaf_sample)}</strong>
              <small>az automatikus CRF mintakódolásaiból · SSIM {formatNumber(summary.average_ssim, 4)} · PSNR {formatNumber(summary.average_psnr_db)} dB</small>
            </Card>
            <Card className="stat-card">
              <span className="eyebrow">Kódolási sebesség</span>
              <strong>{formatNumber(summary.average_encode_fps)} fps</strong>
              <small>{formatNumber(summary.average_realtime_factor)}× valós idő · összesen {formatNumber(summary.encode_hours, 1)} óra kódolás</small>
            </Card>
            <Card className="stat-card">
              <span className="eyebrow">Munkák</span>
              <strong>{summary.jobs}</strong>
              <small>átlagos CRF {formatNumber(summary.average_crf)} · {Object.entries(summary.encoders).map(([name, count]) => `${name}: ${count}`).join(" · ") || "—"}</small>
            </Card>
          </div>

          <Card className="stats-table-card">
            <div className="stats-toolbar">
              <label className="field">
                <span>Rendezés</span>
                <select value={sortKey} onChange={(event) => setSortKey(event.target.value as SortKey)} aria-label="Rendezés">
                  {Object.entries(SORT_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
              </label>
              <Button variant="ghost" onClick={() => setDescending((value) => !value)} aria-pressed={descending}>{descending ? "Csökkenő" : "Növekvő"}</Button>
            </div>
            <div className="table-scroll">
              <table className="stats-table">
                <thead>
                  <tr>
                    <th scope="col">Munka</th>
                    <th scope="col">Kodek</th>
                    <th scope="col">CRF</th>
                    <th scope="col">Forrás → kimenet</th>
                    <th scope="col">Megtakarítás</th>
                    <th scope="col">VMAF</th>
                    <th scope="col">SSIM / PSNR</th>
                    <th scope="col">Sebesség</th>
                    <th scope="col">Idő</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.job_id}>
                      <th scope="row"><Link to={`/jobs/${encodeURIComponent(row.job_id)}`}>{row.name}</Link><small>{formatDate(row.finished_at)}</small></th>
                      <td>{row.encoder ?? "—"}{row.preset ? <small>{row.preset}</small> : null}</td>
                      <td>{formatNumber(row.crf)}{row.auto_crf ? <Badge tone="info">auto</Badge> : null}</td>
                      <td>{formatBytes(row.source_bytes)} → {formatBytes(row.output_bytes)}</td>
                      <td>{row.saved_percent == null ? "—" : <>{formatPercent(row.saved_percent)}<small>{formatBytes(row.saved_bytes)}</small></>}</td>
                      <td>{formatNumber(row.quality.vmaf_sample)}</td>
                      <td>{formatNumber(row.quality.ssim_mean, 4)} / {formatNumber(row.quality.psnr_mean_db)} dB</td>
                      <td>{row.encode_fps == null ? "—" : <>{formatNumber(row.encode_fps)} fps<small>{formatNumber(row.realtime_factor)}×</small></>}</td>
                      <td>{formatDuration(row.encode_seconds)}<small>összesen {formatDuration(row.total_seconds)}</small></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="muted">A „—” hiányzó bizonyítékot jelent (például régebbi kiadással készült munka); a rendszer nem becsül. A VMAF csak az automatikus CRF mintakódolásaira vonatkozik, nem a teljes filmre.</p>
          </Card>
        </>
      )}
    </div>
  );
}

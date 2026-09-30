import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { formatBytes, formatDuration, formatNumber, formatPercent } from "../utils";
import { Card } from "./ui";

/** Size, speed and quality of one completed job (overview sidebar). */
export function JobStatisticsCard({ jobId }: { jobId: string }) {
  const query = useQuery({ queryKey: ["job-statistics", jobId], queryFn: () => api.jobStatistics(jobId), retry: false });
  const stats = query.data;
  if (!stats) return null;
  return (
    <Card>
      <span className="eyebrow">Statisztika</span>
      <dl className="summary-list summary-list--stacked">
        <div><dt>Forrás → kimenet</dt><dd>{formatBytes(stats.source_bytes)} → {formatBytes(stats.output_bytes)}</dd></div>
        <div><dt>Megtakarítás</dt><dd>{stats.saved_percent == null ? "—" : `${formatPercent(stats.saved_percent)} (${formatBytes(stats.saved_bytes)})`}</dd></div>
        <div><dt>Átlagos bitráta</dt><dd>{stats.bitrate_kbps == null ? "—" : `${formatNumber(stats.bitrate_kbps, 0)} kb/s`}</dd></div>
        <div><dt>Kódoló</dt><dd>{stats.encoder ?? "—"} · CRF {formatNumber(stats.crf)}{stats.preset ? ` · ${stats.preset}` : ""}</dd></div>
        <div><dt>Kódolási sebesség</dt><dd>{stats.encode_fps == null ? "—" : `${formatNumber(stats.encode_fps)} fps (${formatNumber(stats.realtime_factor)}× valós idő)`}</dd></div>
        <div><dt>Kódolás ideje</dt><dd>{formatDuration(stats.encode_seconds)}</dd></div>
        <div><dt>VMAF (minta)</dt><dd>{stats.quality.vmaf_sample == null ? "—" : `${formatNumber(stats.quality.vmaf_sample)}${stats.quality.vmaf_target ? ` / cél ${formatNumber(stats.quality.vmaf_target, 1)}` : ""}`}</dd></div>
        <div><dt>SSIM / PSNR</dt><dd>{formatNumber(stats.quality.ssim_mean, 4)} / {formatNumber(stats.quality.psnr_mean_db)} dB</dd></div>
      </dl>
    </Card>
  );
}

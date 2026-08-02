import type { ContentType, Job, JobState } from "./api/types";

export const STATE_LABELS: Record<JobState, string> = {
  QUEUED: "Várólistán",
  SCANNING: "Lemez elemzése",
  AWAITING_SELECTION: "Beállításra vár",
  READY: "Indításra kész",
  ENCODING: "Videó kódolása",
  MUXING: "MKV összeállítása",
  QC: "Minőség-ellenőrzés",
  COMPARISON: "Kép-összehasonlítás",
  UPLOADING: "Képek feltöltése",
  COMPLETED: "Elkészült",
  FAILED: "Hibás",
  CANCELLED: "Megszakítva",
  NEEDS_REVIEW: "Ellenőrzést kér",
  UPLOAD_FAILED: "Feltöltési hiba",
};

export const CONTENT_LABELS: Record<ContentType, string> = {
  FILM: "Film",
  CONCERT: "Koncert",
  ANIME: "Anime",
  SERIES: "Sorozat",
};

const PIPELINE: JobState[] = [
  "QUEUED",
  "SCANNING",
  "AWAITING_SELECTION",
  "READY",
  "ENCODING",
  "MUXING",
  "QC",
  "COMPARISON",
  "UPLOADING",
  "COMPLETED",
];

export function stateTone(state: JobState): "neutral" | "info" | "success" | "warning" | "danger" {
  if (state === "COMPLETED") return "success";
  if (state === "FAILED" || state === "CANCELLED" || state === "UPLOAD_FAILED") return "danger";
  if (state === "NEEDS_REVIEW" || state === "AWAITING_SELECTION") return "warning";
  if (["SCANNING", "ENCODING", "MUXING", "QC", "COMPARISON", "UPLOADING"].includes(state)) {
    return "info";
  }
  return "neutral";
}

export function stageProgress(job: Job): number {
  if (typeof job.progress === "number") return Math.max(0, Math.min(1, job.progress));
  const index = PIPELINE.indexOf(job.state);
  if (index < 0) return 0;
  return index / (PIPELINE.length - 1);
}

export function isActiveState(state: JobState): boolean {
  return !["QUEUED", "COMPLETED", "FAILED", "CANCELLED"].includes(state);
}

export function isTerminalState(state: JobState): boolean {
  return ["COMPLETED", "FAILED", "CANCELLED"].includes(state);
}

export function formatDate(value: string | null, withTime = true): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("hu-HU", {
    year: "numeric",
    month: "short",
    day: "2-digit",
    ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}),
  }).format(date);
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const total = Math.max(0, Math.round(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`
    : `${minutes}:${String(secs).padStart(2, "0")}`;
}

export function formatBytes(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  if (value < 1024) return `${value} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let number = value;
  let index = -1;
  do {
    number /= 1024;
    index += 1;
  } while (number >= 1024 && index < units.length - 1);
  return `${number.toFixed(number >= 10 ? 1 : 2)} ${units[index]}`;
}

export function humanize(value: string): string {
  return value
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function basename(path: string): string {
  return path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || path;
}

export function suggestedOutputName(name: string, encoder: "x264" | "x265"): string {
  const safe = name
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^A-Za-z0-9._ -]+/g, "")
    .trim()
    .replace(/[ .]+/g, ".")
    .replace(/^\.+|\.+$/g, "");
  return `${safe || "Encode"}.BluRay.${encoder}`;
}

export function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text);
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand("copy");
  textarea.remove();
  return Promise.resolve();
}

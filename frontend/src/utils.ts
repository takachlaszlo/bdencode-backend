import type { ContentType, Job, JobState } from "./api/types";

export const STATE_LABELS: Record<JobState, string> = {
  QUEUED: "Várólistán",
  SCANNING: "Lemez elemzése",
  AWAITING_SELECTION: "Beállításra vár",
  READY: "Előkészítés",
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

const PIPELINE_BASELINES: Partial<Record<JobState, number>> = {
  QUEUED: 0,
  SCANNING: 0.02,
  AWAITING_SELECTION: 0.10,
  READY: 0.12,
  ENCODING: 0.15,
  MUXING: 0.78,
  QC: 0.85,
  COMPARISON: 0.92,
  UPLOADING: 0.98,
  COMPLETED: 1,
};

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
  const fallbackState = job.resume_state
    ?? (job.state === "UPLOAD_FAILED" ? "UPLOADING" : job.state);
  return PIPELINE_BASELINES[fallbackState] ?? 0;
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

const EVENT_KIND_LABELS: Record<string, string> = {
  "job.created": "Munka létrehozva",
  "job.state": "Állapotváltozás",
  "job.selection": "Beállítások jóváhagyva",
  "job.progress": "Előrehaladás",
  "job.retry": "Folytatás elindítva",
  "job.workspace-cleaned": "Ideiglenes munkafájlok törölve",
  "job.workspace-cleanup-warning": "Az ideiglenes munkafájlok takarítása nem sikerült",
  "scan.created": "Lemezvizsgálat létrehozva",
  "scan.state": "Lemezvizsgálat állapota",
  "artifact.created": "Melléklet létrehozva",
};

const EVENT_MESSAGE_LABELS: Record<string, string> = {
  "claimed by worker": "A worker megkezdte a lemezvizsgálatot",
  "scan complete; playlist, processing and tracks require confirmation": "A lemezvizsgálat elkészült; a playlist, a feldolgozás és a sávok jóváhagyásra várnak",
  "selection accepted": "A beállítások elfogadva",
  "reference timeline prepared": "A referencia-idővonal elkészült",
  "video encode complete": "A videókódolás elkészült",
  "final Matroska mux complete": "A végleges Matroska összeállítása elkészült",
  "container and audio QC passed": "A konténer- és hangellenőrzés sikeres",
  "I/P/B comparison complete": "Az I/P/B összehasonlítás elkészült",
  "encode, QC and comparison completed": "A kódolás, az ellenőrzés és az összehasonlítás elkészült",
  "image upload failed; retry is safe": "A képfeltöltés sikertelen; biztonságosan újrapróbálható",
};

export function formatStatusMessage(message: string | null, fallback: string): string {
  if (!message) return fallback;
  const retry = /^retrying failed ([A-Z_]+) stage$/.exec(message);
  if (retry) {
    const state = retry[1] as JobState;
    return `${STATE_LABELS[state] ?? retry[1]}: biztonságos folytatás`;
  }
  return EVENT_MESSAGE_LABELS[message] ?? formatWorkerError(message);
}

export function formatWorkerError(error: string): string {
  if (error.includes("chapters.xml")) {
    return "A fejezetlista létrehozása sikertelen volt. A kész videó- és hangsávok megmaradtak; a javítás után biztonságosan folytatható.";
  }
  if (error.includes("subtitle.mks") || error.includes("-subtitle.mks")) {
    return "Egy feliratsáv Matroska-fájlja nem készült el. A kész videó- és hangsávok megmaradtak; a javítás után biztonságosan folytatható.";
  }
  return error;
}

export function formatEventMessage(kind: string, message: string | null): string {
  if (message && message !== kind) return formatStatusMessage(message, "");
  return EVENT_KIND_LABELS[kind] ?? humanize(kind.replaceAll(".", "_"));
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

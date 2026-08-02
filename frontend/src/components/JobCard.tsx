import { ArrowRight, Check, Clock3, Disc3 } from "lucide-react";
import { Link } from "react-router";
import type { Job } from "../api/types";
import { CONTENT_LABELS, formatDate, stageProgress, STATE_LABELS, stateTone } from "../utils";
import { Badge, ProgressBar } from "./ui";

export function JobCard({ job, compact = false }: { job: Job; compact?: boolean }) {
  const progress = stageProgress(job);
  return (
    <Link to={`/jobs/${job.id}`} className={compact ? "job-row" : "job-card"}>
      <div className="job-card__icon" aria-hidden="true">
        <Disc3 size={compact ? 20 : 24} />
      </div>
      <div className="job-card__body">
        <div className="job-card__heading">
          <div>
            <strong>{job.name}</strong>
            <span>{CONTENT_LABELS[job.content_type]} · {job.disc_type === "AUTO" ? "Automatikus lemeztípus" : job.disc_type}</span>
          </div>
          <Badge tone={stateTone(job.state)}>{STATE_LABELS[job.state]}</Badge>
        </div>
        {!compact && (
          <>
            <ProgressBar value={progress} />
            <div className="job-card__meta">
              <span><Clock3 size={14} aria-hidden="true" /> {formatDate(job.updated_at)}</span>
              <span className="job-card__message">{job.status_message || "Munkafolyamat előkészítve"}</span>
            </div>
          </>
        )}
      </div>
      <ArrowRight className="job-card__arrow" size={18} aria-hidden="true" />
    </Link>
  );
}

export function PipelineSteps({ job }: { job: Job }) {
  const stages = [
    ["SCANNING", "Scan"],
    ["READY", "Előkészítés"],
    ["ENCODING", "Kódolás"],
    ["MUXING", "Mux"],
    ["QC", "QC"],
    ["COMPARISON", "Comparison"],
    ["UPLOADING", "Feltöltés"],
    ["COMPLETED", "Kész"],
  ] as const;
  const order = stages.map(([state]) => state);
  const paused = ["AWAITING_SELECTION", "NEEDS_REVIEW", "UPLOAD_FAILED"].includes(job.state);
  const effectiveState = job.state === "AWAITING_SELECTION"
    ? "READY"
    : job.state === "NEEDS_REVIEW"
      ? job.resume_state ?? "READY"
      : job.state === "UPLOAD_FAILED"
        ? "UPLOADING"
        : job.state;
  const effectiveIndex = order.indexOf(effectiveState as (typeof order)[number]);
  const inferred = effectiveIndex >= 0 ? effectiveIndex : 0;

  return (
    <ol className="pipeline" aria-label="Kódolási folyamat">
      {stages.map(([state, label], index) => {
        const complete = job.state === "COMPLETED" || inferred > index;
        const active = job.state !== "COMPLETED" && (state === job.state || (paused && index === inferred));
        return (
          <li key={state} className={complete ? "pipeline__step pipeline__step--complete" : active ? "pipeline__step pipeline__step--active" : "pipeline__step"}>
            <span {...(complete ? { role: "img", "aria-label": "Kész" } : {})}>{complete ? <Check size={13} aria-hidden="true" /> : index + 1}</span>
            <small>{label}</small>
          </li>
        );
      })}
    </ol>
  );
}

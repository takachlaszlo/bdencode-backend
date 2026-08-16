import { ArrowRight, Check, Clock3, Disc3, MoreHorizontal, Pause, Play } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router";
import type { Job } from "../api/types";
import { CONTENT_LABELS, formatDate, formatStatusMessage, stageProgress, STATE_LABELS, stateTone } from "../utils";
import { Badge, ProgressBar } from "./ui";

type JobControlState = "RUNNING" | "PAUSE_REQUESTED" | "PAUSED" | "CANCEL_REQUESTED";
type ControllableJob = Job & {
  allowed_operations?: string[];
  control_state?: JobControlState;
};

export interface JobCardProps {
  job: Job;
  compact?: boolean;
  onAction?: (action: string, job: Job) => void;
  pendingAction?: string | null;
}

const ACTION_LABELS: Record<string, string> = {
  pause: "Szüneteltetés",
  resume: "Folytatás",
  cancel: "Megszakítás",
  retry_failed: "Folytatás a hibától",
  restart_cancelled: "Újraindítás",
  cleanup: "Takarítás",
  delete: "Törlés",
  prepare_release: "Release előkészítése",
  delete_release: "Completed release törlése",
};

function jobControl(job: Job): ControllableJob {
  return job as ControllableJob;
}

function statusPresentation(job: Job) {
  const controlState = jobControl(job).control_state;
  if (controlState === "PAUSED") return { label: "Szüneteltetve", tone: "warning" as const };
  if (controlState === "PAUSE_REQUESTED") return { label: "Szüneteltetés folyamatban", tone: "warning" as const };
  if (controlState === "CANCEL_REQUESTED") return { label: "Megszakítás folyamatban", tone: "danger" as const };
  return { label: STATE_LABELS[job.state], tone: stateTone(job.state) };
}

function JobActionControls({
  job,
  onAction,
  pendingAction,
  compact,
}: Pick<JobCardProps, "job" | "onAction" | "pendingAction"> & { compact: boolean }) {
  const [menuOpen, setMenuOpen] = useState(false);
  const menuId = useId();
  const containerRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuItemRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const operations = jobControl(job).allowed_operations;
  const allowedOperations = Array.isArray(operations)
    ? operations.filter(
        (operation): operation is string => typeof operation === "string" && operation.length > 0,
      )
    : [];
  const quickAction = allowedOperations.includes("resume")
    ? "resume"
    : allowedOperations.includes("pause")
      ? "pause"
      : null;
  const menuActions = allowedOperations.filter((operation) => operation !== quickAction);
  const busy = pendingAction != null;

  useEffect(() => {
    if (!menuOpen) return;
    const closeOutside = (event: PointerEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) setMenuOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    return () => document.removeEventListener("pointerdown", closeOutside);
  }, [menuOpen]);

  useEffect(() => {
    if (busy) setMenuOpen(false);
  }, [busy]);

  if (!onAction || allowedOperations.length === 0) return null;

  const runAction = (action: string) => {
    setMenuOpen(false);
    onAction(action, job);
  };
  const focusMenuItem = (index: number) => {
    const items = menuItemRefs.current.filter((item): item is HTMLButtonElement => item != null);
    if (items.length === 0) return;
    items[(index + items.length) % items.length]?.focus();
  };
  const openAndFocus = (index: number) => {
    setMenuOpen(true);
    queueMicrotask(() => focusMenuItem(index));
  };

  return (
    <div
      ref={containerRef}
      className={compact ? "job-card__controls job-card__controls--compact" : "job-card__controls"}
      aria-busy={busy || undefined}
    >
      {quickAction && (
        <button
          type="button"
          className="job-card__quick icon-button"
          aria-label={`${job.name}: ${ACTION_LABELS[quickAction].toLocaleLowerCase("hu-HU")}`}
          title={ACTION_LABELS[quickAction]}
          disabled={busy}
          onClick={() => runAction(quickAction)}
        >
          {quickAction === "resume" ? <Play size={16} aria-hidden="true" /> : <Pause size={16} aria-hidden="true" />}
        </button>
      )}
      {menuActions.length > 0 && (
        <>
          <button
            ref={triggerRef}
            type="button"
            className="job-card__menu-trigger icon-button"
            aria-label={`${job.name}: további műveletek`}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            aria-controls={menuId}
            disabled={busy}
            onClick={() => setMenuOpen((open) => !open)}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown") {
                event.preventDefault();
                openAndFocus(0);
              } else if (event.key === "ArrowUp") {
                event.preventDefault();
                openAndFocus(-1);
              }
            }}
          >
            <MoreHorizontal size={17} aria-hidden="true" />
          </button>
          {menuOpen && (
            <div id={menuId} className="job-card__menu" role="menu" aria-label={`${job.name} műveletei`}>
              {menuActions.map((action, index) => (
                <button
                  key={action}
                  ref={(element) => { menuItemRefs.current[index] = element; }}
                  type="button"
                  role="menuitem"
                  className={["delete", "delete_release", "cancel"].includes(action) ? "job-card__menu-item job-card__menu-item--danger" : "job-card__menu-item"}
                  disabled={busy}
                  onClick={() => runAction(action)}
                  onKeyDown={(event) => {
                    if (event.key === "Escape") {
                      event.preventDefault();
                      setMenuOpen(false);
                      triggerRef.current?.focus();
                    } else if (event.key === "ArrowDown") {
                      event.preventDefault();
                      focusMenuItem(index + 1);
                    } else if (event.key === "ArrowUp") {
                      event.preventDefault();
                      focusMenuItem(index - 1);
                    } else if (event.key === "Home") {
                      event.preventDefault();
                      focusMenuItem(0);
                    } else if (event.key === "End") {
                      event.preventDefault();
                      focusMenuItem(-1);
                    }
                  }}
                >
                  {ACTION_LABELS[action] ?? action}
                </button>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

export function JobCard({ job, compact = false, onAction, pendingAction }: JobCardProps) {
  const progress = stageProgress(job);
  const titleId = useId();
  const status = statusPresentation(job);
  return (
    <article className={compact ? "job-row" : "job-card"} aria-labelledby={titleId}>
      <Link to={`/jobs/${encodeURIComponent(job.id)}`} className="job-card__details" aria-label={`${job.name} részletei`}>
        <div className="job-card__icon" aria-hidden="true">
          <Disc3 size={compact ? 20 : 24} />
        </div>
        <div className="job-card__body">
          <div className="job-card__heading">
            <div>
              <strong id={titleId}>{job.name}</strong>
              <span>{CONTENT_LABELS[job.content_type]} · {job.disc_type === "AUTO" ? "Automatikus lemeztípus" : job.disc_type}</span>
            </div>
            <Badge tone={status.tone}>{status.label}</Badge>
          </div>
          {!compact && (
            <>
              <ProgressBar value={progress} />
              <div className="job-card__meta">
                <span><Clock3 size={14} aria-hidden="true" /> {formatDate(job.updated_at)}</span>
                <span className="job-card__message">{formatStatusMessage(job.status_message, "Munkafolyamat előkészítve")}</span>
              </div>
            </>
          )}
        </div>
        <ArrowRight className="job-card__arrow" size={18} aria-hidden="true" />
      </Link>
      <JobActionControls job={job} compact={compact} onAction={onAction} pendingAction={pendingAction} />
    </article>
  );
}

export function PipelineSteps({ job }: { job: Job }) {
  const stages = [
    ["SCANNING", "Scan"],
    ["READY", "Sorban áll"],
    ["ENCODING", "Kódolás"],
    ["MUXING", "Mux"],
    ["QC", "QC"],
    ["COMPARISON", "Comparison"],
    ["UPLOADING", "Feltöltés"],
    ["COMPLETED", "Kész"],
  ] as const;
  const order = stages.map(([state]) => state);
  const controlState = jobControl(job).control_state;
  const controlPaused = controlState === "PAUSED" || controlState === "PAUSE_REQUESTED";
  const paused = controlPaused || ["AWAITING_SELECTION", "NEEDS_REVIEW", "UPLOAD_FAILED"].includes(job.state);
  const effectiveState = controlPaused
    ? job.resume_state ?? job.state
    : job.state === "AWAITING_SELECTION"
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
        const active = job.state !== "COMPLETED" && (paused ? index === inferred : state === job.state);
        return (
          <li
            key={state}
            className={complete ? "pipeline__step pipeline__step--complete" : active ? "pipeline__step pipeline__step--active" : "pipeline__step"}
            aria-current={active ? "step" : undefined}
          >
            <span {...(complete ? { role: "img", "aria-label": "Kész" } : {})}>{complete ? <Check size={13} aria-hidden="true" /> : index + 1}</span>
            <small>{label}</small>
          </li>
        );
      })}
    </ol>
  );
}

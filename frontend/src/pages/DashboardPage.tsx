import { useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  CheckCircle2,
  CirclePlus,
  Clock3,
  Cpu,
  Disc3,
  HardDrive,
  ListOrdered,
  ShieldCheck,
} from "lucide-react";
import { Link } from "react-router";
import { api } from "../api/client";
import type { Job } from "../api/types";
import { JobCard } from "../components/JobCard";
import { Badge, Card, EmptyState, LoadingPanel, PageHeader, ProgressBar } from "../components/ui";
import { formatDate, formatStatusMessage, isActiveState, stageProgress, STATE_LABELS, stateTone } from "../utils";

function nestedNumber(value: unknown, ...keys: string[]): number | null {
  let current: unknown = value;
  for (const key of keys) {
    if (!current || typeof current !== "object" || !(key in current)) return null;
    current = (current as Record<string, unknown>)[key];
  }
  return typeof current === "number" ? current : null;
}

function findActive(jobs: Job[], activeId: string | null | undefined): Job | undefined {
  return jobs.find((job) => job.id === activeId) ?? jobs.find((job) => isActiveState(job.state));
}

export function DashboardPage() {
  const health = useQuery({ queryKey: ["health"], queryFn: api.health, refetchInterval: 5000 });
  const jobs = useQuery({ queryKey: ["jobs", "dashboard"], queryFn: () => api.jobs(undefined, 100), refetchInterval: 5000 });
  const runtime = useQuery({ queryKey: ["runtime-capabilities"], queryFn: api.runtimeCapabilities, staleTime: 60_000 });
  const capabilities = useQuery({ queryKey: ["capabilities"], queryFn: api.capabilities, staleTime: 60_000 });

  const allJobs = jobs.data?.items ?? [];
  const active = findActive(allJobs, health.data?.active_job_id);
  const queued = allJobs.filter((job) => ["QUEUED", "SCANNING", "AWAITING_SELECTION", "READY"].includes(job.state));
  const recent = allJobs.filter((job) => ["COMPLETED", "FAILED", "CANCELLED"].includes(job.state)).slice(0, 4);
  const logicalCpus = nestedNumber(runtime.data, "host", "logical_cpus");
  const freeBytes = nestedNumber(runtime.data, "paths", "data", "free_bytes");
  const cpuFraction = capabilities.data?.constraints.cpu_budget_fraction;

  return (
    <div className="page page--dashboard">
      <PageHeader
        eyebrow="Kezelőpult"
        title="Mit kódolunk ma?"
        description="A teljes Blu-ray munkafolyamat egyetlen áttekinthető felületen."
        actions={
          <Link className="button button--primary" to="/new">
            <CirclePlus size={18} />
            <span>Új kódolás</span>
          </Link>
        }
      />

      <div className="dashboard-grid dashboard-grid--top">
        <Link to="/new" className="hero-action-card">
          <span className="hero-action-card__orb"><Disc3 size={30} /></span>
          <div>
            <span className="eyebrow">Új munka</span>
            <h2>Blu-ray hozzáadása</h2>
            <p>Válassz forrást, majd a rendszer végigvezet a lemezen, sávokon és beállításokon.</p>
          </div>
          <span className="hero-action-card__link">Kezdés <ArrowRight size={17} /></span>
        </Link>

        <Card className="active-card">
          <div className="card-heading">
            <div>
              <span className="eyebrow">Aktív munka</span>
              <h2>{active ? active.name : "A kódoló szabad"}</h2>
            </div>
            {active ? <Badge tone={stateTone(active.state)}>{STATE_LABELS[active.state]}</Badge> : <Badge tone="success">Készen áll</Badge>}
          </div>
          {jobs.isLoading || health.isLoading ? (
            <LoadingPanel />
          ) : active ? (
            <>
              <div className="active-card__progress-number">{Math.round(stageProgress(active) * 100)}<small>% · teljes</small></div>
              <ProgressBar value={stageProgress(active)} label={formatStatusMessage(active.status_message, STATE_LABELS[active.state])} />
              <Link to={`/jobs/${active.id}`} className="text-link">Részletek megnyitása <ArrowRight size={15} /></Link>
            </>
          ) : (
            <div className="active-card__idle">
              <CheckCircle2 size={28} />
              <div><strong>Nincs futó feladat</strong><span>A következő várólistás munka automatikusan indul.</span></div>
            </div>
          )}
        </Card>

        <Card className="system-card">
          <div className="card-heading">
            <div>
              <span className="eyebrow">Szerver</span>
              <h2>Rendszerállapot</h2>
            </div>
            <span
              className={health.isSuccess
                ? "live-indicator"
                : health.isError
                  ? "live-indicator live-indicator--error"
                  : "live-indicator live-indicator--pending"}
              role="status"
            >
              {health.isSuccess ? "Online" : health.isError ? "Nem elérhető" : "Kapcsolódás…"}
            </span>
          </div>
          <div className="system-metrics">
            <div><Cpu size={18} /><span><strong>{typeof cpuFraction === "number" ? `${Math.round(cpuFraction * 100)}%` : "80%"}</strong>CPU-keret</span></div>
            <div><ShieldCheck size={18} /><span><strong>{logicalCpus ?? "—"}</strong>logikai CPU</span></div>
            <div><HardDrive size={18} /><span><strong>{freeBytes !== null ? `${(freeBytes / 1024 ** 4).toFixed(1)} TiB` : "—"}</strong>szabad hely</span></div>
          </div>
          <Link to="/settings" className="text-link">Rendszer részletei <ArrowRight size={15} /></Link>
        </Card>
      </div>

      <div className="dashboard-grid dashboard-grid--content">
        <Card className="queue-panel">
          <div className="section-heading">
            <div>
              <span className="section-heading__icon"><ListOrdered size={19} /></span>
              <div><h2>Várólista</h2><p>{queued.length} munka előkészítés alatt vagy kódolásra kész</p></div>
            </div>
            <Link to="/queue" className="text-link">Összes megnyitása <ArrowRight size={15} /></Link>
          </div>
          {jobs.isLoading ? <LoadingPanel /> : queued.length ? (
            <div className="job-list job-list--compact">
              {queued.slice(0, 5).map((job) => <JobCard key={job.id} job={job} compact />)}
            </div>
          ) : (
            <EmptyState
              icon={<Clock3 size={25} />}
              title="A várólista üres"
              description="Adj hozzá több filmet; a scan és a beállítás előre elkészülhet, az encode-ok pedig egymás után futnak."
              action={<Link className="button button--secondary" to="/new">Munka hozzáadása</Link>}
            />
          )}
        </Card>

        <Card className="recent-panel">
          <div className="section-heading">
            <div><h2>Legutóbbi munkák</h2><p>Elkészült és lezárt kódolások</p></div>
            <Link to="/archive" className="text-link">Archívum <ArrowRight size={15} /></Link>
          </div>
          {recent.length ? (
            <div className="recent-list">
              {recent.map((job) => (
                <Link key={job.id} to={`/jobs/${job.id}`} className="recent-item">
                  <span className="recent-item__icon"><Disc3 size={18} /></span>
                  <span><strong>{job.name}</strong><small>{formatDate(job.finished_at || job.updated_at)}</small></span>
                  <Badge tone={stateTone(job.state)}>{STATE_LABELS[job.state]}</Badge>
                </Link>
              ))}
            </div>
          ) : (
            <EmptyState title="Még nincs előzmény" description="Az első lezárt kódolás itt fog megjelenni." />
          )}
        </Card>
      </div>
    </div>
  );
}

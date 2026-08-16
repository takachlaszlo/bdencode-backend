import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  CalendarClock,
  CheckCircle2,
  ClipboardList,
  Code2,
  Disc3,
  Download,
  File,
  FileJson,
  FileText,
  FolderOpen,
  Gauge,
  HardDrive,
  Images,
  Info,
  ListChecks,
  LoaderCircle,
  MoreHorizontal,
  PackageCheck,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
  Settings2,
  ShieldCheck,
  StopCircle,
  Trash2,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router";
import { api, ApiError, artifactContentUrl, fetchArtifactText } from "../api/client";
import type { Artifact, DiscScanResult, EventRecord, Job, JobOperation, JobStorageReport, ReleasePreparation, ReleasePreparationList, Scan } from "../api/types";
import { ComparisonPanel } from "../components/ComparisonPanel";
import { PipelineSteps } from "../components/JobCard";
import { ReleasePanel } from "../components/ReleasePanel";
import { SelectionWizard } from "../components/SelectionWizard";
import { Badge, Button, Card, EmptyState, LoadingPanel, Modal, Notice, PageHeader, ProgressBar } from "../components/ui";
import { normalizeStoredSelection } from "../selection";
import { CONTENT_LABELS, formatBytes, formatDate, formatEventMessage, formatStatusMessage, formatWorkerError, humanize, isFastComparisonTimeoutReview, stageProgress, STATE_LABELS, stateTone } from "../utils";

type Tab = "overview" | "settings" | "comparison" | "release" | "events" | "files";
type JobDetailLocationState = {
  newlyCreated?: boolean;
  retryStarted?: boolean;
};

interface CompletedReleaseDeleteSnapshot {
  id: string;
  version: number;
  manifest: {
    releaseName: string;
    sha256: string;
  };
  state: string;
  preparationVersions: Record<string, number>;
}

const tabs: Array<{ value: Tab; label: string; icon: typeof Info }> = [
  { value: "overview", label: "Áttekintés", icon: Gauge },
  { value: "settings", label: "Beállítások", icon: Settings2 },
  { value: "comparison", label: "Comparison", icon: Images },
  { value: "release", label: "Release", icon: PackageCheck },
  { value: "events", label: "Események", icon: ClipboardList },
  { value: "files", label: "Fájlok és logok", icon: FolderOpen },
];

const RETRYABLE_FAILED_STATES = new Set(["READY", "ENCODING", "MUXING", "QC", "COMPARISON"]);

function fallbackOperations(job: Job): string[] {
  if (job.state === "COMPLETED") return ["delete"];
  if (job.state === "FAILED") return ["retry_failed", "delete"];
  if (job.state === "CANCELLED") return ["restart_cancelled", "delete"];
  if (!job.control_state) return [];
  if (job.control_state === "PAUSED") return ["resume", "cancel"];
  if (job.control_state === "PAUSE_REQUESTED") return ["cancel"];
  if (job.control_state === "CANCEL_REQUESTED") return [];
  return ["pause", "cancel"];
}

function operationsFor(job: Job): Set<string> {
  const operations = Array.isArray(job.allowed_operations)
    ? job.allowed_operations.filter((operation): operation is string => typeof operation === "string")
    : fallbackOperations(job);
  return new Set(operations);
}

function controlStatus(job: Job): { label: string; message: string; tone: "neutral" | "info" | "success" | "warning" | "danger" } {
  if (job.control_state === "PAUSED") return { label: "Szüneteltetve", message: job.control_message || "A munka biztonságos ponton vár a folytatásra.", tone: "warning" };
  if (job.control_state === "PAUSE_REQUESTED") return { label: "Szüneteltetés folyamatban", message: job.control_message || "A worker a következő biztonságos ponton állítja meg a munkát.", tone: "warning" };
  if (job.control_state === "CANCEL_REQUESTED") return { label: "Megszakítás folyamatban", message: job.control_message || "A worker rendezetten lezárja a futó folyamatot.", tone: "danger" };
  return { label: STATE_LABELS[job.state], message: formatStatusMessage(job.status_message, "Állapotfrissítésre vár"), tone: stateTone(job.state) };
}

function latestSuccessfulScan(scans: Scan[]): Scan | undefined {
  return scans.find((scan) => ["AWAITING_SELECTION", "COMPLETED"].includes(scan.status));
}

function imageUploadLabel(value: string | null): string {
  if (value === "imgbb") return "Csak ImgBB";
  if (value === "catbox") return "Csak Catbox";
  if (value === "freeimage") return "Csak Freeimage";
  return "Automatikus tartalékkal";
}

function releasePreparationsFrom(
  value: ReleasePreparationList | ReleasePreparation[] | undefined,
): ReleasePreparation[] {
  if (Array.isArray(value)) return value;
  if (!value) return [];
  if (Array.isArray(value.items)) return value.items;
  return Array.isArray(value.preparations) ? value.preparations : [];
}

function releasePreparationVersions(
  value: ReleasePreparationList | ReleasePreparation[] | undefined,
): Record<string, number> {
  const entries = releasePreparationsFrom(value).flatMap((preparation) => {
    const id = typeof preparation.id === "string"
      ? preparation.id
      : typeof preparation.preparation_id === "string"
        ? preparation.preparation_id
        : null;
    return id && typeof preparation.version === "number" ? [[id, preparation.version] as const] : [];
  });
  return Object.fromEntries(entries.sort(([left], [right]) => left.localeCompare(right)));
}

function completedReleaseDeleteSnapshot(
  job: Job | undefined,
  output: Artifact | undefined,
  preparations: ReleasePreparationList | ReleasePreparation[] | undefined,
): CompletedReleaseDeleteSnapshot | null {
  if (!job || job.state !== "COMPLETED" || !output?.sha256 || preparations === undefined) return null;
  const filename = output.name || output.path.split(/[\\/]/).at(-1) || job.name;
  return {
    id: job.id,
    version: job.version,
    manifest: {
      releaseName: filename.replace(/\.mkv$/i, ""),
      sha256: output.sha256,
    },
    state: job.state,
    preparationVersions: releasePreparationVersions(preparations),
  };
}

export function JobDetailPage() {
  const { jobId = "" } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const locationState = location.state as JobDetailLocationState | null;
  const requestedParams = new URLSearchParams(location.search);
  const requestedTab = requestedParams.get("tab") as Tab | null;
  const requestedAction = requestedParams.get("action");
  const [tab, setTab] = useState<Tab>(requestedTab && tabs.some((item) => item.value === requestedTab) ? requestedTab : "overview");
  const [cancelOpen, setCancelOpen] = useState(false);
  const [retryOpen, setRetryOpen] = useState(false);
  const [restartOpen, setRestartOpen] = useState(false);
  const [purgeOpen, setPurgeOpen] = useState(false);
  const [cleanupOpen, setCleanupOpen] = useState(false);
  const [releaseDeleteTarget, setReleaseDeleteTarget] = useState<CompletedReleaseDeleteSnapshot | null>(null);
  const [purgeConfirmation, setPurgeConfirmation] = useState("");
  const [releaseDeleteConfirmation, setReleaseDeleteConfirmation] = useState("");
  const [forceSeededReleaseDelete, setForceSeededReleaseDelete] = useState(false);
  const operationMenuRef = useRef<HTMLDetailsElement>(null);
  const requestedReleaseDeleteHandled = useRef(false);
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const queryClient = useQueryClient();
  const jobQuery = useQuery({ queryKey: ["job", jobId], queryFn: () => api.job(jobId), refetchInterval: 4000 });
  const scansQuery = useQuery({ queryKey: ["scans", jobId], queryFn: () => api.scans(jobId), refetchInterval: 5000 });
  const artifactsQuery = useQuery({ queryKey: ["artifacts", jobId], queryFn: () => api.artifacts(jobId), refetchInterval: 7000 });
  const eventsQuery = useQuery({ queryKey: ["events", jobId], queryFn: () => api.events(jobId), refetchInterval: 4000 });
  const storageQuery = useQuery({ queryKey: ["job-storage", jobId], queryFn: () => api.jobStorage(jobId), refetchInterval: 15_000, retry: false });
  const releasePreparationsQuery = useQuery({
    queryKey: ["release-preparations", jobId],
    queryFn: () => api.releasePreparations(jobId),
    enabled: jobQuery.data?.state === "COMPLETED",
    refetchInterval: 10_000,
  });
  const outputArtifact = artifactsQuery.data?.items.find((artifact) => artifact.kind === "OUTPUT");

  useEffect(() => {
    if (requestedAction !== "delete-release" || requestedReleaseDeleteHandled.current) return;
    const snapshot = completedReleaseDeleteSnapshot(
      jobQuery.data,
      outputArtifact,
      releasePreparationsQuery.data,
    );
    if (!snapshot) return;
    requestedReleaseDeleteHandled.current = true;
    setReleaseDeleteTarget(snapshot);
  }, [jobQuery.data, outputArtifact, releasePreparationsQuery.data, requestedAction]);
  const control = useMutation({
    mutationFn: ({ action, revision }: { action: Extract<JobOperation, "pause" | "resume" | "cancel">; revision?: number }) => {
      if (action === "pause") return api.pauseJob(jobId, revision);
      if (action === "resume") return api.continueJob(jobId, revision);
      return api.requestCancelJob(jobId, revision);
    },
    onSuccess: (updatedJob, variables) => {
      if (variables.action === "cancel") setCancelOpen(false);
      queryClient.setQueryData(["job", jobId], updatedJob);
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ["job", jobId] }),
        queryClient.invalidateQueries({ queryKey: ["jobs"] }),
        queryClient.invalidateQueries({ queryKey: ["events", jobId] }),
      ]);
    },
  });
  const retryUpload = useMutation({
    mutationFn: () => api.retryUpload(jobId),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["job", jobId] }),
  });
  const retryJob = useMutation({
    mutationFn: (expectedVersion: number) => api.retryJob(jobId, expectedVersion),
    onSuccess: (retriedJob) => {
      setRetryOpen(false);
      queryClient.setQueryData(["job", jobId], retriedJob);
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ["job", jobId] }),
        queryClient.invalidateQueries({ queryKey: ["jobs"] }),
        queryClient.invalidateQueries({ queryKey: ["events", jobId] }),
      ]);
      navigate(`/jobs/${encodeURIComponent(jobId)}`, {
        replace: true,
        state: { retryStarted: true } satisfies JobDetailLocationState,
      });
    },
  });
  const restartJob = useMutation({
    mutationFn: (expectedVersion: number) => api.restartJob(jobId, expectedVersion),
    onSuccess: (restartedJob) => {
      setRestartOpen(false);
      queryClient.setQueryData(["job", jobId], restartedJob);
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ["job", jobId] }),
        queryClient.invalidateQueries({ queryKey: ["jobs"] }),
        queryClient.invalidateQueries({ queryKey: ["events", jobId] }),
      ]);
      navigate(`/jobs/${encodeURIComponent(jobId)}`, {
        replace: true,
        state: { retryStarted: true } satisfies JobDetailLocationState,
      });
    },
  });
  const purgeJob = useMutation({
    mutationFn: (expectedVersion: number) => api.purgeJob(jobId, expectedVersion),
    onSuccess: () => {
      setPurgeOpen(false);
      queryClient.removeQueries({ queryKey: ["job", jobId] });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      navigate("/archive", { replace: true });
    },
  });
  const cleanupJob = useMutation({
    mutationFn: (expectedVersion: number) => api.cleanupJob(jobId, expectedVersion),
    onSuccess: () => {
      setCleanupOpen(false);
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ["job", jobId] }),
        queryClient.invalidateQueries({ queryKey: ["job-storage", jobId] }),
        queryClient.invalidateQueries({ queryKey: ["events", jobId] }),
        queryClient.invalidateQueries({ queryKey: ["artifacts", jobId] }),
      ]);
    },
  });
  const deleteRelease = useMutation({
    mutationFn: (request: { confirmation: string; expected_sha256: string; force_if_seeded: boolean; preparation_versions: Record<string, number> }) => api.deleteJobRelease(jobId, request),
    onSuccess: () => {
      setReleaseDeleteTarget(null);
      setReleaseDeleteConfirmation("");
      setForceSeededReleaseDelete(false);
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ["job", jobId] }),
        queryClient.invalidateQueries({ queryKey: ["job-storage", jobId] }),
        queryClient.invalidateQueries({ queryKey: ["artifacts", jobId] }),
        queryClient.invalidateQueries({ queryKey: ["release-preparations", jobId] }),
      ]);
    },
  });
  const resumeComparison = useMutation({
    mutationFn: () => api.resumeJob(jobId),
    onSuccess: (resumedJob) => {
      queryClient.setQueryData(["job", jobId], resumedJob);
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ["job", jobId] }),
        queryClient.invalidateQueries({ queryKey: ["jobs"] }),
        queryClient.invalidateQueries({ queryKey: ["events", jobId] }),
      ]);
      navigate(`/jobs/${encodeURIComponent(jobId)}`, {
        replace: true,
        state: { retryStarted: true } satisfies JobDetailLocationState,
      });
    },
  });

  function openRetryConfirmation() {
    retryJob.reset();
    setRetryOpen(true);
  }

  function closeRetryConfirmation() {
    if (retryJob.isPending) return;
    retryJob.reset();
    setRetryOpen(false);
  }

  if (jobQuery.isLoading) return <div className="page"><LoadingPanel label="Munka betöltése…" /></div>;
  if (jobQuery.isError || !jobQuery.data) return (
    <div className="page"><Notice tone="danger" title="A munka nem nyitható meg">{jobQuery.error instanceof Error ? jobQuery.error.message : "Ismeretlen hiba"}</Notice><Link className="button button--secondary" to="/queue"><ArrowLeft size={17} /> Vissza</Link></div>
  );

  const job = jobQuery.data;
  const scanRow = latestSuccessfulScan(scansQuery.data?.items ?? []);
  const scan = scanRow?.result && "playlists" in scanRow.result ? scanRow.result as DiscScanResult : null;
  const artifacts = artifactsQuery.data?.items ?? [];
  const events = eventsQuery.data?.items ?? [];
  const latestWorkspaceEvent = [...events].reverse().find((event) => ["job.workspace-cleaned", "job.workspace-cleanup-warning"].includes(event.kind));
  const workspaceCleaned = latestWorkspaceEvent?.kind === "job.workspace-cleaned";
  const workspaceCleanupWarning = latestWorkspaceEvent?.kind === "job.workspace-cleanup-warning";
  const allowedOperations = operationsFor(job);
  const currentControlStatus = controlStatus(job);
  const controlRevision = job.control_revision;
  const retryableFailure = job.state === "FAILED"
    && job.resume_state !== null
    && RETRYABLE_FAILED_STATES.has(job.resume_state)
    && allowedOperations.has("retry_failed");
  const comparisonTimeoutReview = job.state === "NEEDS_REVIEW"
    && job.resume_state === "COMPARISON"
    && isFastComparisonTimeoutReview(job.status_message);
  const configurable = ["AWAITING_SELECTION", "NEEDS_REVIEW"].includes(job.state)
    && !comparisonTimeoutReview
    && scan;
  const terminal = ["COMPLETED", "FAILED", "CANCELLED"].includes(job.state);
  const canCleanup = allowedOperations.has("cleanup")
    && (storageQuery.data?.cleanup_allowed ?? (storageQuery.data?.reclaimable_bytes ?? 0) > 0);
  const releasePresent = storageQuery.data?.release_present
    ?? ((storageQuery.data?.completed_release_bytes ?? 0) > 0);
  const canDeleteRelease = job.state === "COMPLETED" && releasePresent;
  const completedOutput = artifacts.find((artifact) => artifact.kind === "OUTPUT");
  const completedOutputSha256 = completedOutput?.sha256 ?? "";
  const releaseName = (completedOutput?.name || completedOutput?.path.split(/[\\/]/).at(-1) || job.output_path?.split(/[\\/]/).at(-1) || job.name).replace(/\.mkv$/i, "");

  function openCompletedReleaseDelete() {
    const snapshot = completedReleaseDeleteSnapshot(job, completedOutput, releasePreparationsQuery.data);
    if (!snapshot) return;
    deleteRelease.reset();
    setReleaseDeleteConfirmation("");
    setForceSeededReleaseDelete(false);
    setReleaseDeleteTarget(snapshot);
  }

  return (
    <div className="page page--job-detail">
      <Link className="back-link" to={terminal ? "/archive" : "/queue"}><ArrowLeft size={16} /> {terminal ? "Vissza az archívumhoz" : "Vissza a várólistához"}</Link>
      <PageHeader
        eyebrow={`${CONTENT_LABELS[job.content_type]} · ${job.disc_type}`}
        title={job.name}
        description={currentControlStatus.message}
        actions={
          <div className="header-actions">
            <Badge tone={currentControlStatus.tone}>{currentControlStatus.label}</Badge>
            {allowedOperations.has("resume") && <Button icon={<Play size={17} />} loading={control.isPending && control.variables?.action === "resume"} disabled={control.isPending} onClick={() => control.mutate({ action: "resume", revision: controlRevision })}>Folytatás</Button>}
            {allowedOperations.has("pause") && <Button icon={<Pause size={17} />} loading={control.isPending && control.variables?.action === "pause"} disabled={control.isPending} onClick={() => control.mutate({ action: "pause", revision: controlRevision })}>Szüneteltetés</Button>}
            {(allowedOperations.size > (allowedOperations.has("pause") || allowedOperations.has("resume") ? 1 : 0) || job.state === "UPLOAD_FAILED" || canDeleteRelease) && (
              <details ref={operationMenuRef} className="action-menu">
                <summary className="button button--secondary"><MoreHorizontal size={17} /><span>Műveletek</span></summary>
                <div className="action-menu__popover" aria-label="Munka műveletei">
                  {retryableFailure && <button type="button" onClick={() => { operationMenuRef.current?.removeAttribute("open"); openRetryConfirmation(); }}><RotateCcw size={16} />Folytatás a hibától</button>}
                  {allowedOperations.has("restart_cancelled") && <button type="button" onClick={() => { operationMenuRef.current?.removeAttribute("open"); restartJob.reset(); setRestartOpen(true); }}><RotateCcw size={16} />Újraindítás</button>}
                  {job.state === "UPLOAD_FAILED" && <button type="button" disabled={retryUpload.isPending} onClick={() => { operationMenuRef.current?.removeAttribute("open"); retryUpload.mutate(); }}><RefreshCw size={16} />Feltöltés újra</button>}
                  {allowedOperations.has("prepare_release") && <button type="button" onClick={() => { operationMenuRef.current?.removeAttribute("open"); setTab("release"); }}><PackageCheck size={16} />Release előkészítése</button>}
                  {canCleanup && <button type="button" onClick={() => { operationMenuRef.current?.removeAttribute("open"); cleanupJob.reset(); setCleanupOpen(true); }}><HardDrive size={16} />Ideiglenes fájlok takarítása</button>}
                  {allowedOperations.has("cancel") && <button className="action-menu__danger" type="button" onClick={() => { operationMenuRef.current?.removeAttribute("open"); control.reset(); setCancelOpen(true); }}><StopCircle size={16} />Megszakítás kérése</button>}
                  {allowedOperations.has("delete") && <button className="action-menu__danger" type="button" onClick={() => { operationMenuRef.current?.removeAttribute("open"); purgeJob.reset(); setPurgeConfirmation(""); setPurgeOpen(true); }}><Trash2 size={16} />Munka törlése</button>}
                  {allowedOperations.has("delete_release") && canDeleteRelease && <button className="action-menu__danger" type="button" disabled={!completedOutputSha256 || releasePreparationsQuery.data === undefined} onClick={() => { operationMenuRef.current?.removeAttribute("open"); openCompletedReleaseDelete(); }}><Trash2 size={16} />Completed release törlése</button>}
                </div>
              </details>
            )}
          </div>
        }
      />

      {control.isError && <Notice tone="danger" title="A vezérlési kérés sikertelen">{control.error instanceof ApiError ? control.error.detail : control.error.message}</Notice>}
      {job.control_state === "PAUSE_REQUESTED" && <Notice tone="warning" title="A szüneteltetés kérése rögzítve">A worker a futó eszközt biztonságosan lezárja. A félkész szakasz folytatáskor újraindulhat.</Notice>}
      {job.control_state === "PAUSED" && <Notice tone="info" title="A munka szünetel">Az ellenőrzött checkpointok és munkafájlok megmaradtak. A Folytatás visszaadja a munkát a workernek.</Notice>}
      {job.control_state === "CANCEL_REQUESTED" && <Notice tone="warning" title="Rendezett megszakítás folyamatban">A job csak a futó folyamat lezárása után kerül Megszakítva állapotba.</Notice>}

      {locationState?.newlyCreated && job.state === "QUEUED" && (
        <Notice tone="success" title="A munka létrejött">A worker hamarosan elkezdi a lemez scanjét. Ezután itt választhatod ki a playlistet és a sávokat.</Notice>
      )}
      {locationState?.retryStarted && !terminal && (
        <Notice tone="success" title="A munka folytatása elindult">A worker az ellenőrzött checkpointok alapján folytatja a feldolgozást.</Notice>
      )}
      {job.error && <Notice tone="danger" title="A feldolgozás hibát jelzett"><p>{formatWorkerError(job.error)}</p><details><summary>Technikai részletek</summary><pre>{job.error}</pre></details></Notice>}
      {retryableFailure && (
        <Notice tone="warning" title="A munkafájlok a biztonságos folytatáshoz megmaradtak">
          A takarítás szándékosan vár: a rendszer megőrzi az érvényes checkpointokat és az elkészült részeredményeket. Folytatáskor csak a hiányzó vagy érvénytelen szakaszok futnak újra. Sikeres véglegesítés után a nagyméretű ideiglenes <code>work</code> mappa automatikusan törlődik; a logok, elemzések és comparison mellékletek megmaradnak.
        </Notice>
      )}
      {job.state === "COMPLETED" && workspaceCleaned && (
        <Notice tone="success" title="A kódolás lezárult és a munkaterület kitakarítva">
          A végleges MKV és a kiadható comparison bizonyítékok a completed mappába kerültek. A belső logok, útvonalak és teljes manifest kizárólag a privát munka auditjában maradtak meg; a nagyméretű ideiglenes fájlok törlődtek.
        </Notice>
      )}
      {job.state === "COMPLETED" && workspaceCleanupWarning && (
        <Notice tone="warning" title="A kódolás elkészült, de maradtak ideiglenes fájlok">
          A végleges MKV biztonságban van. A munkaterület takarítása nem sikerült; a részletek az eseménynaplóban találhatók.
        </Notice>
      )}
      {comparisonTimeoutReview && (
        <Notice tone="warning" title="A gyors comparison időkorlátja lejárt">
          <p>{formatStatusMessage(job.status_message, "A comparison biztonságosan folytatható.")}</p>
          <p>A már elkészült képpárok és ellenőrzött checkpointok megmaradtak; a kódolást és a muxot nem kell újrafuttatni.</p>
          <Button icon={<RefreshCw size={17} />} loading={resumeComparison.isPending} onClick={() => resumeComparison.mutate()}>Folytatás a comparisontól</Button>
          {resumeComparison.isError && <p>{resumeComparison.error instanceof ApiError ? resumeComparison.error.detail : resumeComparison.error.message}</p>}
        </Notice>
      )}
      {job.state === "NEEDS_REVIEW" && !comparisonTimeoutReview && <Notice tone="warning" title="Operátori ellenőrzés szükséges">{formatStatusMessage(job.status_message, "A munkafolyamat csak a beállítások felülvizsgálata után folytatható.")}</Notice>}

      <Card className="job-progress-card">
        <div className="job-progress-card__top">
          <div><span className="job-progress-card__disc"><Disc3 size={23} /></span><div><strong>{currentControlStatus.label}</strong><span>{currentControlStatus.message}</span>{job.control_requested_at && job.control_state !== "RUNNING" && <small>Kérés ideje: {formatDate(job.control_requested_at)}</small>}</div></div>
          <div className="job-progress-card__percent"><strong>{Math.round(stageProgress(job) * 100)}%</strong><small>teljes folyamat</small></div>
        </div>
        <ProgressBar value={stageProgress(job)} label={`${Math.round(stageProgress(job) * 100)}% · ${currentControlStatus.label}`} />
        <PipelineSteps job={job} />
      </Card>

      <StorageCard
        report={storageQuery.data}
        loading={storageQuery.isLoading}
        error={storageQuery.isError ? storageQuery.error : null}
        canCleanup={canCleanup}
        cleanupPending={cleanupJob.isPending}
        onCleanup={() => { cleanupJob.reset(); setCleanupOpen(true); }}
        onRefresh={() => void storageQuery.refetch()}
      />

      <div className="tabs" role="tablist">
        {tabs.map(({ value, label, icon: Icon }, index) => (
          <button
            ref={(element) => { tabRefs.current[index] = element; }}
            id={`job-tab-${value}`}
            key={value}
            role="tab"
            aria-selected={tab === value}
            aria-controls={`job-panel-${value}`}
            tabIndex={tab === value ? 0 : -1}
            className={tab === value ? "tab tab--active" : "tab"}
            onClick={() => setTab(value)}
            onKeyDown={(event) => {
              let next = index;
              if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
              else if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
              else if (event.key === "Home") next = 0;
              else if (event.key === "End") next = tabs.length - 1;
              else return;
              event.preventDefault();
              setTab(tabs[next].value);
              tabRefs.current[next]?.focus();
            }}
          >
            <Icon size={17} />{label}
            {value === "files" && artifacts.length > 0 && <span>{artifacts.length}</span>}
          </button>
        ))}
      </div>

      <div id={`job-panel-${tab}`} className="tab-panel" role="tabpanel" aria-labelledby={`job-tab-${tab}`}>
        {tab === "overview" && (
          <Overview job={job} scan={scan} events={events} artifacts={artifacts} onConfigure={() => setTab("settings")} />
        )}
        {tab === "settings" && (
          configurable ? (
            <SelectionWizard job={job} scan={scan} onComplete={() => setTab("overview")} />
          ) : job.selection ? (
            <SavedSelection job={job} scan={scan} />
          ) : (
            <EmptyState icon={job.state === "SCANNING" ? <LoaderCircle className="spin" size={28} /> : <Settings2 size={28} />} title={job.state === "SCANNING" ? "A scan folyamatban van" : "Még nincs jóváhagyott beállítás"} description="A playlist- és sávválasztó a scan befejezése után jelenik meg." />
          )
        )}
        {tab === "comparison" && <ComparisonPanel artifacts={artifacts} />}
        {tab === "release" && <ReleasePanel job={job} outputArtifact={completedOutput} />}
        {tab === "events" && <EventTimeline events={events} loading={eventsQuery.isLoading} />}
        {tab === "files" && <ArtifactsPanel artifacts={artifacts} />}
      </div>

      <Modal
        open={cancelOpen}
        title="Biztosan megszakítod?"
        busy={control.isPending}
        ariaDescribedBy="cancel-job-description"
        onClose={() => { if (!control.isPending) setCancelOpen(false); }}
        footer={<><Button variant="ghost" disabled={control.isPending} onClick={() => setCancelOpen(false)}>Mégse</Button><Button variant="danger" icon={<StopCircle size={17} />} loading={control.isPending} onClick={() => control.mutate({ action: "cancel", revision: controlRevision })}>Megszakítás kérése</Button></>}
      >
        <div id="cancel-job-description"><Notice tone="warning">A worker rendezetten lezárja a futó programot; a job csak ezután lesz Megszakítva. A már elkészült munkafájlok és naplók megmaradnak.</Notice></div>
        {control.isError && <Notice tone="danger">{control.error instanceof ApiError ? control.error.detail : control.error.message}</Notice>}
      </Modal>

      <Modal
        open={retryOpen}
        title="Folytatod a hibától?"
        busy={retryJob.isPending}
        onClose={closeRetryConfirmation}
        footer={<><Button variant="ghost" disabled={retryJob.isPending} onClick={closeRetryConfirmation}>Mégse</Button><Button icon={<RotateCcw size={17} />} loading={retryJob.isPending} onClick={() => retryJob.mutate(job.version)}>Folytatás a hibától</Button></>}
      >
        <Notice tone="warning" title="A kész munka nem vész el">
          Az újraindítás az érvényes szakasz-checkpointokat és a már elkészült munkafájlokat újrahasználja. A worker az első hiányzó vagy érvénytelen szakasztól folytatja, a hibás szakaszt pedig szükség szerint újrafuttatja.
        </Notice>
        {retryJob.isError && <Notice tone="danger" title="A folytatás nem indítható">{retryJob.error instanceof ApiError ? retryJob.error.detail : retryJob.error.message}</Notice>}
      </Modal>

      <Modal
        open={restartOpen}
        title="Újraindítod a megszakított munkát?"
        busy={restartJob.isPending}
        onClose={() => { if (!restartJob.isPending) setRestartOpen(false); }}
        footer={<><Button variant="ghost" disabled={restartJob.isPending} onClick={() => setRestartOpen(false)}>Mégse</Button><Button icon={<RotateCcw size={17} />} loading={restartJob.isPending} onClick={() => restartJob.mutate(job.version)}>Újraindítás</Button></>}
      >
        <Notice tone="info" title="A korábbi beállítások megmaradnak">
          Ha a scan és a jóváhagyott beállítások már elkészültek, a munka kész paraméterekkel visszakerül a várólistára és felhasználja az érvényes checkpointokat. Korábbi megszakításnál a scan indul újra.
        </Notice>
        {restartJob.isError && <Notice tone="danger" title="A munka nem indítható újra">{restartJob.error instanceof ApiError ? restartJob.error.detail : restartJob.error.message}</Notice>}
      </Modal>

      <Modal
        open={purgeOpen}
        title="Végleg törlöd ezt a munkát?"
        busy={purgeJob.isPending}
        ariaDescribedBy="purge-job-description"
        onClose={() => { if (!purgeJob.isPending) setPurgeOpen(false); }}
        footer={<><Button variant="ghost" disabled={purgeJob.isPending} onClick={() => setPurgeOpen(false)}>Mégse</Button><Button variant="danger" icon={<Trash2 size={17} />} loading={purgeJob.isPending} disabled={purgeConfirmation !== job.name} onClick={() => purgeJob.mutate(job.version)}>Munka végleges törlése</Button></>}
      >
        <div id="purge-job-description"><Notice tone="danger" title="Ez nem vonható vissza">A privát jobrekord, munkafájlok, checkpointok, logok és mellékletek törlődnek. A forráslemezhez és a completed release-hez a rendszer nem nyúl.</Notice></div>
        <label className="field confirmation-field">A megerősítéshez írd be a munka nevét:<input autoComplete="off" value={purgeConfirmation} onChange={(event) => setPurgeConfirmation(event.target.value)} /><small><code>{job.name}</code></small></label>
        {purgeJob.isError && <Notice tone="danger" title="A munka nem törölhető">{purgeJob.error instanceof ApiError ? purgeJob.error.detail : purgeJob.error.message}</Notice>}
      </Modal>

      <Modal
        open={cleanupOpen}
        title="Kitakarítod az ideiglenes fájlokat?"
        busy={cleanupJob.isPending}
        ariaDescribedBy="cleanup-job-description"
        onClose={() => { if (!cleanupJob.isPending) setCleanupOpen(false); }}
        footer={<><Button variant="ghost" disabled={cleanupJob.isPending} onClick={() => setCleanupOpen(false)}>Mégse</Button><Button icon={<HardDrive size={17} />} loading={cleanupJob.isPending} disabled={(storageQuery.data?.reclaimable_bytes ?? 0) <= 0} onClick={() => cleanupJob.mutate(job.version)}>Takarítás</Button></>}
      >
        <div id="cleanup-job-description"><Notice tone="info" title={`${formatBytes(storageQuery.data?.reclaimable_bytes)} szabadítható fel`}>Csak a sikeresen lezárt munka nagyméretű ideiglenes <code>work</code> tartalma törlődik. A jobrekord, logok, audit, comparison és completed release megmarad.</Notice></div>
        {cleanupJob.isError && <Notice tone="danger" title="A takarítás sikertelen">{cleanupJob.error instanceof ApiError ? cleanupJob.error.detail : cleanupJob.error.message}</Notice>}
      </Modal>

      <Modal
        open={releaseDeleteTarget !== null}
        title="Végleg törlöd a completed release-t?"
        busy={deleteRelease.isPending}
        ariaDescribedBy="delete-release-description"
        onClose={() => { if (!deleteRelease.isPending) setReleaseDeleteTarget(null); }}
        footer={<><Button variant="ghost" disabled={deleteRelease.isPending} onClick={() => setReleaseDeleteTarget(null)}>Mégse</Button><Button variant="danger" icon={<Trash2 size={17} />} loading={deleteRelease.isPending} disabled={!releaseDeleteTarget || releaseDeleteConfirmation !== releaseDeleteTarget.manifest.releaseName || !canDeleteRelease} onClick={() => releaseDeleteTarget && deleteRelease.mutate({ confirmation: releaseDeleteTarget.manifest.releaseName, expected_sha256: releaseDeleteTarget.manifest.sha256, force_if_seeded: forceSeededReleaseDelete, preparation_versions: releaseDeleteTarget.preparationVersions })}>Release végleges törlése</Button></>}
      >
        <div id="delete-release-description"><Notice tone="danger" title="Ez a kész MKV-t is törli">A completed release teljes publikus csomagja eltűnik. A forráslemez és a privát job auditja megmarad; a korábbi torrent-előkészítések érvénytelenné válhatnak.</Notice></div>
        {releaseDeleteTarget && <dl className="summary-list summary-list--stacked"><div><dt>Rögzített release</dt><dd>{releaseDeleteTarget.manifest.releaseName}</dd></div><div><dt>Állapot / job revízió</dt><dd>{releaseDeleteTarget.state} · v{releaseDeleteTarget.version}</dd></div><div><dt>OUTPUT SHA-256</dt><dd><code>{releaseDeleteTarget.manifest.sha256}</code></dd></div><div><dt>Rögzített előkészítések</dt><dd><code>{JSON.stringify(releaseDeleteTarget.preparationVersions)}</code></dd></div></dl>}
        <label className="field confirmation-field">A megerősítéshez írd be a release nevét:<input autoComplete="off" value={releaseDeleteConfirmation} onChange={(event) => setReleaseDeleteConfirmation(event.target.value)} /><small><code>{releaseDeleteTarget?.manifest.releaseName ?? releaseName}</code></small></label>
        <label className="toggle-row toggle-row--compact"><span><strong>Kényszerített törlés külső vagy seedelt eredmény ellenére</strong><small>Csak akkor kapcsold be, ha a qBittorrent-, tracker- vagy bizonytalan hálózati eredményt ellenőrizted, és a külső példányokat is tudatosan kezeled.</small></span><input type="checkbox" checked={forceSeededReleaseDelete} onChange={(event) => setForceSeededReleaseDelete(event.target.checked)} /><span className="toggle" aria-hidden="true" /></label>
        {!storageQuery.isLoading && !canDeleteRelease && <Notice tone="warning">A completed release már nem található, ezért nincs törölhető cél.</Notice>}
        {!releaseDeleteTarget?.manifest.sha256 && <Notice tone="warning">A törlés le van tiltva, mert a kimeneti MKV elvárt SHA-256 értéke nem érhető el.</Notice>}
        {deleteRelease.isError && <Notice tone="danger" title="A release nem törölhető">{deleteRelease.error instanceof ApiError ? deleteRelease.error.detail : deleteRelease.error.message}</Notice>}
      </Modal>
    </div>
  );
}

function StorageCard({
  report,
  loading,
  error,
  canCleanup,
  cleanupPending,
  onCleanup,
  onRefresh,
}: {
  report?: JobStorageReport;
  loading: boolean;
  error: Error | null;
  canCleanup: boolean;
  cleanupPending: boolean;
  onCleanup: () => void;
  onRefresh: () => void;
}) {
  return (
    <Card className="job-storage-card">
      <div className="section-heading">
        <div><span className="section-heading__icon"><HardDrive size={19} /></span><div><h2>Tárhely és takarítás</h2><p>A job privát munkaterülete és a külön kezelt completed release</p></div></div>
        {report && <Badge tone={report.reclaimable_bytes > 0 ? "warning" : "success"}>{report.reclaimable_bytes > 0 ? `${formatBytes(report.reclaimable_bytes)} felszabadítható` : "Nincs maradék"}</Badge>}
      </div>
      {loading ? <LoadingPanel label="Tárhely számítása…" /> : error ? (
        <Notice tone="warning" title="A tárhelyadat nem olvasható"><Button variant="ghost" icon={<RefreshCw size={15} />} onClick={onRefresh}>Újrapróbálás</Button></Notice>
      ) : report ? (
        <>
          <div className="storage-facts">
            <div><small>Privát munkaterület</small><strong>{formatBytes(report.workspace_bytes)}</strong></div>
            <div><small>Ideiglenes, törölhető</small><strong>{formatBytes(report.reclaimable_bytes)}</strong></div>
            <div><small>Completed release</small><strong>{formatBytes(report.completed_release_bytes)}</strong></div>
          </div>
          <div className="storage-category-list" aria-label="Munkaterület kategóriái">
            {report.categories.filter((item) => item.present).map((item) => <span key={item.name}><strong>{humanize(item.name)}</strong><small>{formatBytes(item.bytes)} · {item.file_count} fájl{item.reclaimable ? " · takarítható" : ""}</small></span>)}
          </div>
          {canCleanup && <Button variant="secondary" icon={<HardDrive size={16} />} loading={cleanupPending} disabled={report.reclaimable_bytes <= 0} onClick={onCleanup}>Ideiglenes fájlok takarítása</Button>}
        </>
      ) : null}
    </Card>
  );
}

function Overview({ job, scan, events, artifacts, onConfigure }: { job: Job; scan: DiscScanResult | null; events: EventRecord[]; artifacts: Artifact[]; onConfigure: () => void }) {
  const newest = events.slice(-4).reverse();
  return (
    <div className="overview-grid">
      <div className="overview-main">
        {["AWAITING_SELECTION", "NEEDS_REVIEW"].includes(job.state) && !isFastComparisonTimeoutReview(job.status_message) && scan && (
          <Card className="action-callout">
            <span className="action-callout__icon"><ListChecks size={25} /></span>
            <div><span className="eyebrow">Te következel</span><h2>{job.state === "AWAITING_SELECTION" ? "Válaszd ki a filmet és a sávokat" : "Vizsgáld felül a beállításokat"}</h2><p>{scan.playlists.length} playlistet találtam. A kódolás addig nem indul el, amíg a tervet jóvá nem hagyod.</p></div>
            <Button icon={<Play size={17} />} onClick={onConfigure}>Beállítások megnyitása</Button>
          </Card>
        )}
        <Card>
          <div className="section-heading"><div><span className="section-heading__icon"><ClipboardList size={19} /></span><div><h2>Legutóbbi események</h2><p>Sanitizált, tartós állapotnapló</p></div></div></div>
          {newest.length ? <div className="mini-timeline">{newest.map((event) => <EventItem key={event.id} event={event} compact />)}</div> : <p className="muted">Még nincs naplózott esemény.</p>}
        </Card>
      </div>
      <aside className="overview-side">
        <Card>
          <span className="eyebrow">Munka adatai</span>
          <dl className="summary-list summary-list--stacked">
            <div><dt>Forrás</dt><dd title={job.source_path}>{job.source_path}</dd></div>
            <div><dt>Létrehozva</dt><dd>{formatDate(job.created_at)}</dd></div>
            <div><dt>Frissítve</dt><dd>{formatDate(job.updated_at)}</dd></div>
            <div><dt>Azonosító</dt><dd><code>{job.id}</code></dd></div>
            <div><dt>Mellékletek</dt><dd>{artifacts.length}</dd></div>
          </dl>
        </Card>
        {scan && <Card><span className="eyebrow">Lemez scan</span><dl className="summary-list summary-list--stacked"><div><dt>Típus</dt><dd>{scan.disc_kind.toUpperCase()}</dd></div><div><dt>Playlistek</dt><dd>{scan.playlists.length}</dd></div><div><dt>Több változat</dt><dd>{scan.has_multiple_editions ? "Igen" : "Nem"}</dd></div><div><dt>3D észlelve</dt><dd>{scan.has_three_d ? "Igen — nem támogatott" : "Nem"}</dd></div></dl></Card>}
      </aside>
    </div>
  );
}

function SavedSelection({ job, scan }: { job: Job; scan: DiscScanResult | null }) {
  const selection = normalizeStoredSelection(job.selection);
  if (!selection) {
    return <Notice tone="warning" title="A mentett selection nem olvasható">A nyers selection JSON nem objektum. A worker operátori ellenőrzést fog kérni.</Notice>;
  }
  const settings = selection.settings;
  return (
    <div className="saved-selection">
      <Notice tone={isFastComparisonTimeoutReview(job.status_message) ? "info" : "success"} title={isFastComparisonTimeoutReview(job.status_message) ? "A jóváhagyott terv változatlan" : "A terv jóváhagyva"}>{isFastComparisonTimeoutReview(job.status_message) ? "A selection módosítása nem szükséges. A comparison az áttekintő lapon folytatható az érvényes checkpointoktól." : "Nincs külön indítógomb: a munka kész paraméterekkel vár a sorára, majd a worker automatikusan végigviszi. A comparison adatok külön mellékletek maradnak."}</Notice>
      <div className="saved-selection-grid">
        <Card><span className="eyebrow">Kép és kódoló</span><dl className="summary-list summary-list--stacked"><div><dt>Playlist</dt><dd>{selection.playlistId ?? "—"}</dd></div><div><dt>Kódoló</dt><dd>{scan?.disc_kind === "uhd" ? "x265" : "x264"}</dd></div><div><dt>Részletesség</dt><dd>{selection.detailLevel ?? "—"}</dd></div><div><dt>CRF</dt><dd>{String(settings.crf ?? "ajánlott")}</dd></div><div><dt>Preset</dt><dd>{String(settings.preset ?? "ajánlott")}</dd></div><div><dt>Filter</dt><dd>{selection.temporalFilter ?? "—"}</dd></div></dl></Card>
        <Card><span className="eyebrow">Kimenet</span><dl className="summary-list summary-list--stacked"><div><dt>Fájlnév</dt><dd>{selection.outputName ? `${selection.outputName}.mkv` : "—"}</dd></div><div><dt>Sávok</dt><dd>{selection.tracks.filter((track) => track.action !== "omit").length} megtartva</dd></div><div><dt>Képfeltöltés</dt><dd>{selection.uploadImages === null ? "—" : selection.uploadImages ? imageUploadLabel(selection.imageUploadProvider) : "Kikapcsolva"}</dd></div><div><dt>I/P/B egyezés</dt><dd>{selection.dualTypeMatch === false ? "Kötelező · régi mentés felülbírálva" : "Kötelező"}</dd></div></dl></Card>
        <Card className="saved-json"><details><summary><Code2 size={17} /> Teljes selection JSON</summary><pre>{JSON.stringify(job.selection, null, 2)}</pre></details></Card>
      </div>
    </div>
  );
}

function EventTimeline({ events, loading }: { events: EventRecord[]; loading: boolean }) {
  if (loading) return <LoadingPanel />;
  if (!events.length) return <EmptyState icon={<CalendarClock size={28} />} title="Még nincs esemény" description="A worker állapotváltásai és biztonságos összefoglalói itt jelennek meg." />;
  return <div className="event-timeline">{[...events].reverse().map((event) => <EventItem key={event.id} event={event} />)}</div>;
}

function EventItem({ event, compact = false }: { event: EventRecord; compact?: boolean }) {
  const isError = event.kind.toLowerCase().includes("error") || event.state_to === "FAILED" || event.state_to === "UPLOAD_FAILED";
  const isSuccess = event.state_to === "COMPLETED";
  const uploadDetail = typeof event.payload.detail === "string" ? event.payload.detail : null;
  return (
    <article className={isError ? "event-item event-item--error" : isSuccess ? "event-item event-item--success" : "event-item"}>
      <span className="event-item__marker">{isError ? <AlertTriangle size={15} /> : isSuccess ? <CheckCircle2 size={15} /> : <Info size={14} />}</span>
      <div><div className="event-item__heading"><strong>{formatEventMessage(event.kind, event.message)}</strong><time>{formatDate(event.created_at)}</time></div>{event.state_from && event.state_to && <p>{STATE_LABELS[event.state_from]} → {STATE_LABELS[event.state_to]}</p>}{compact && uploadDetail && <p>{uploadDetail}</p>}{!compact && Object.keys(event.payload).length > 0 && <details><summary>Részletek</summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details>}</div>
    </article>
  );
}

function ArtifactsPanel({ artifacts }: { artifacts: Artifact[] }) {
  const [analysis, setAnalysis] = useState<Record<string, unknown> | null>(null);
  const output = artifacts.find((artifact) => artifact.kind === "OUTPUT");
  const analyze = useMutation({ mutationFn: () => api.analyzeMkv(output!.path), onSuccess: setAnalysis });
  const groups = useMemo(() => Object.entries(artifacts.reduce<Record<string, Artifact[]>>((result, artifact) => {
    (result[artifact.kind] ??= []).push(artifact); return result;
  }, {})), [artifacts]);
  if (!artifacts.length) return <EmptyState icon={<File size={28} />} title="Még nincs melléklet" description="A scan manifestje, logok, elemzések és comparison képek munka közben folyamatosan jelennek meg." />;
  return (
    <div className="artifacts-panel">
      {output && (
        <Card className="mkv-analysis-card">
          <div><span className="mkv-analysis-card__icon"><ShieldCheck size={22} /></span><span><strong>Elkészült MKV elemzése</strong><small>A konténerből kiolvassa a trackeket és a kódoló beállításait; comparison adatot nem keres az MKV-ban.</small></span></div>
          <Button variant="secondary" icon={<RefreshCw size={16} />} loading={analyze.isPending} onClick={() => analyze.mutate()}>MKV elemzése</Button>
          {analyze.isError && <Notice tone="danger">{analyze.error instanceof Error ? analyze.error.message : "Az elemzés sikertelen"}</Notice>}
          {analysis && <details className="analysis-result" open><summary>Elemzési eredmény</summary><pre>{JSON.stringify(analysis, null, 2)}</pre></details>}
        </Card>
      )}
      {groups.map(([kind, items]) => (
        <section key={kind} className="artifact-group">
          <div className="section-heading"><div><span className="section-heading__icon">{kind === "LOG" ? <FileText size={18} /> : kind.includes("COMPARISON") || kind === "SPECTROGRAM" ? <Images size={18} /> : <FileJson size={18} />}</span><div><h3>{artifactGroupLabel(kind)}</h3><p>{items.length} melléklet</p></div></div></div>
          <div className="artifact-list">
            {items.map((artifact) => <ArtifactRow key={artifact.id} artifact={artifact} />)}
          </div>
        </section>
      ))}
    </div>
  );
}

function ArtifactRow({ artifact }: { artifact: Artifact }) {
  const [preview, setPreview] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const canPreview = artifact.mime_type?.startsWith("text/") || artifact.mime_type === "application/json";
  async function loadPreview() {
    if (preview !== null) { setPreview(null); return; }
    setLoading(true);
    try { setPreview(await fetchArtifactText(artifact.id)); } finally { setLoading(false); }
  }
  return (
    <div className="artifact-row">
      <span className="artifact-row__icon">{artifact.mime_type === "image/png" ? <Images size={18} /> : artifact.kind === "LOG" ? <FileText size={18} /> : <File size={18} />}</span>
      <span className="artifact-row__name"><strong>{artifact.name}</strong><small>{artifact.mime_type || artifact.kind} · {formatBytes(artifact.size_bytes)}</small></span>
      <div className="artifact-row__actions">{canPreview && <Button variant="ghost" onClick={() => void loadPreview()} loading={loading}>Előnézet</Button>}<a className="icon-button" href={artifactContentUrl(artifact.id)} target="_blank" rel="noreferrer" aria-label={`${artifact.name} letöltése`}><Download size={17} /></a></div>
      {preview !== null && <pre className="artifact-preview">{preview}</pre>}
    </div>
  );
}

function artifactGroupLabel(kind: string): string {
  const labels: Record<string, string> = { OUTPUT: "Kimeneti fájl", LOG: "Logok", MANIFEST: "Manifestek", MEDIAINFO: "MediaInfo", MKVINFO: "MKV elemzések", VIDEO_COMPARISON: "Videó comparison", AUDIO_COMPARISON: "Audió comparison", SPECTROGRAM: "Spektrumképek", REPORT: "Jelentések", BBCODE: "BBCode", OTHER: "Egyéb" };
  return labels[kind] || kind;
}

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
  Images,
  Info,
  ListChecks,
  LoaderCircle,
  Play,
  RefreshCw,
  RotateCcw,
  Settings2,
  ShieldCheck,
  StopCircle,
} from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useLocation, useParams } from "react-router";
import { api, ApiError, artifactContentUrl, fetchArtifactText } from "../api/client";
import type { Artifact, DiscScanResult, EventRecord, Job, Scan } from "../api/types";
import { ComparisonPanel } from "../components/ComparisonPanel";
import { PipelineSteps } from "../components/JobCard";
import { SelectionWizard } from "../components/SelectionWizard";
import { Badge, Button, Card, EmptyState, LoadingPanel, Modal, Notice, PageHeader, ProgressBar } from "../components/ui";
import { normalizeStoredSelection } from "../selection";
import { CONTENT_LABELS, formatBytes, formatDate, stageProgress, STATE_LABELS, stateTone } from "../utils";

type Tab = "overview" | "settings" | "comparison" | "events" | "files";

const tabs: Array<{ value: Tab; label: string; icon: typeof Info }> = [
  { value: "overview", label: "Áttekintés", icon: Gauge },
  { value: "settings", label: "Beállítások", icon: Settings2 },
  { value: "comparison", label: "Comparison", icon: Images },
  { value: "events", label: "Események", icon: ClipboardList },
  { value: "files", label: "Fájlok és logok", icon: FolderOpen },
];

function latestSuccessfulScan(scans: Scan[]): Scan | undefined {
  return scans.find((scan) => ["AWAITING_SELECTION", "COMPLETED"].includes(scan.status));
}

export function JobDetailPage() {
  const { jobId = "" } = useParams();
  const location = useLocation();
  const requestedTab = new URLSearchParams(location.search).get("tab") as Tab | null;
  const [tab, setTab] = useState<Tab>(requestedTab && tabs.some((item) => item.value === requestedTab) ? requestedTab : "overview");
  const [cancelOpen, setCancelOpen] = useState(false);
  const queryClient = useQueryClient();
  const jobQuery = useQuery({ queryKey: ["job", jobId], queryFn: () => api.job(jobId), refetchInterval: 4000 });
  const scansQuery = useQuery({ queryKey: ["scans", jobId], queryFn: () => api.scans(jobId), refetchInterval: 5000 });
  const artifactsQuery = useQuery({ queryKey: ["artifacts", jobId], queryFn: () => api.artifacts(jobId), refetchInterval: 7000 });
  const eventsQuery = useQuery({ queryKey: ["events", jobId], queryFn: () => api.events(jobId), refetchInterval: 4000 });
  const cancel = useMutation({
    mutationFn: () => api.cancelJob(jobId),
    onSuccess: () => {
      setCancelOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["job", jobId] });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
  });
  const retryUpload = useMutation({
    mutationFn: () => api.retryUpload(jobId),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["job", jobId] }),
  });

  if (jobQuery.isLoading) return <div className="page"><LoadingPanel label="Munka betöltése…" /></div>;
  if (jobQuery.isError || !jobQuery.data) return (
    <div className="page"><Notice tone="danger" title="A munka nem nyitható meg">{jobQuery.error instanceof Error ? jobQuery.error.message : "Ismeretlen hiba"}</Notice><Link className="button button--secondary" to="/queue"><ArrowLeft size={17} /> Vissza</Link></div>
  );

  const job = jobQuery.data;
  const scanRow = latestSuccessfulScan(scansQuery.data?.items ?? []);
  const scan = scanRow?.result && "playlists" in scanRow.result ? scanRow.result as DiscScanResult : null;
  const artifacts = artifactsQuery.data?.items ?? [];
  const events = eventsQuery.data?.items ?? [];
  const configurable = ["AWAITING_SELECTION", "NEEDS_REVIEW"].includes(job.state) && scan;
  const terminal = ["COMPLETED", "FAILED", "CANCELLED"].includes(job.state);

  return (
    <div className="page page--job-detail">
      <Link className="back-link" to={terminal ? "/archive" : "/queue"}><ArrowLeft size={16} /> {terminal ? "Vissza az archívumhoz" : "Vissza a várólistához"}</Link>
      <PageHeader
        eyebrow={`${CONTENT_LABELS[job.content_type]} · ${job.disc_type}`}
        title={job.name}
        description={job.status_message || "A munkafolyamat állapota és minden kapcsolódó melléklet."}
        actions={
          <div className="header-actions">
            <Badge tone={stateTone(job.state)}>{STATE_LABELS[job.state]}</Badge>
            {!terminal && <Button variant="danger" icon={<StopCircle size={17} />} onClick={() => setCancelOpen(true)}>Megszakítás kérése</Button>}
            {job.state === "UPLOAD_FAILED" && <Button variant="secondary" icon={<RotateCcw size={17} />} loading={retryUpload.isPending} onClick={() => retryUpload.mutate()}>Feltöltés újra</Button>}
          </div>
        }
      />

      {(location.state as { newlyCreated?: boolean } | null)?.newlyCreated && job.state === "QUEUED" && (
        <Notice tone="success" title="A munka létrejött">A worker hamarosan elkezdi a lemez scanjét. Ezután itt választhatod ki a playlistet és a sávokat.</Notice>
      )}
      {job.error && <Notice tone="danger" title="A worker hibát jelzett">{job.error}</Notice>}
      {job.state === "NEEDS_REVIEW" && <Notice tone="warning" title="Operátori ellenőrzés szükséges">{job.status_message || "A munkafolyamat csak a beállítások felülvizsgálata után folytatható."}</Notice>}

      <Card className="job-progress-card">
        <div className="job-progress-card__top">
          <div><span className="job-progress-card__disc"><Disc3 size={23} /></span><div><strong>{STATE_LABELS[job.state]}</strong><span>{job.status_message || "Állapotfrissítésre vár"}</span></div></div>
          <strong className="job-progress-card__percent">{Math.round(stageProgress(job) * 100)}%</strong>
        </div>
        <ProgressBar value={stageProgress(job)} />
        <PipelineSteps job={job} />
      </Card>

      <div className="tabs" role="tablist">
        {tabs.map(({ value, label, icon: Icon }) => (
          <button key={value} role="tab" aria-selected={tab === value} className={tab === value ? "tab tab--active" : "tab"} onClick={() => setTab(value)}>
            <Icon size={17} />{label}
            {value === "files" && artifacts.length > 0 && <span>{artifacts.length}</span>}
          </button>
        ))}
      </div>

      <div className="tab-panel" role="tabpanel">
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
        {tab === "events" && <EventTimeline events={events} loading={eventsQuery.isLoading} />}
        {tab === "files" && <ArtifactsPanel artifacts={artifacts} />}
      </div>

      <Modal
        open={cancelOpen}
        title="Biztosan megszakítod?"
        onClose={() => setCancelOpen(false)}
        footer={<><Button variant="ghost" onClick={() => setCancelOpen(false)}>Mégse</Button><Button variant="danger" icon={<StopCircle size={17} />} loading={cancel.isPending} onClick={() => cancel.mutate()}>Megszakítás kérése</Button></>}
      >
        <Notice tone="warning">A rendszer állapotváltással kéri a megszakítást. A már elkészült munkafájlok és naplók megmaradnak az ellenőrizhető lezárásig.</Notice>
        {cancel.isError && <Notice tone="danger">{cancel.error instanceof ApiError ? cancel.error.detail : cancel.error.message}</Notice>}
      </Modal>
    </div>
  );
}

function Overview({ job, scan, events, artifacts, onConfigure }: { job: Job; scan: DiscScanResult | null; events: EventRecord[]; artifacts: Artifact[]; onConfigure: () => void }) {
  const newest = events.slice(-4).reverse();
  return (
    <div className="overview-grid">
      <div className="overview-main">
        {["AWAITING_SELECTION", "NEEDS_REVIEW"].includes(job.state) && scan && (
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
      <Notice tone="success" title="A terv jóváhagyva">A worker ezt a selection manifestet használja. A comparison adatok külön mellékletek maradnak.</Notice>
      <div className="saved-selection-grid">
        <Card><span className="eyebrow">Kép és kódoló</span><dl className="summary-list summary-list--stacked"><div><dt>Playlist</dt><dd>{selection.playlistId ?? "—"}</dd></div><div><dt>Kódoló</dt><dd>{scan?.disc_kind === "uhd" ? "x265" : "x264"}</dd></div><div><dt>Részletesség</dt><dd>{selection.detailLevel ?? "—"}</dd></div><div><dt>CRF</dt><dd>{String(settings.crf ?? "ajánlott")}</dd></div><div><dt>Preset</dt><dd>{String(settings.preset ?? "ajánlott")}</dd></div><div><dt>Filter</dt><dd>{selection.temporalFilter ?? "—"}</dd></div></dl></Card>
        <Card><span className="eyebrow">Kimenet</span><dl className="summary-list summary-list--stacked"><div><dt>Fájlnév</dt><dd>{selection.outputName ? `${selection.outputName}.mkv` : "—"}</dd></div><div><dt>Sávok</dt><dd>{selection.tracks.filter((track) => track.action !== "omit").length} megtartva</dd></div><div><dt>ImgBB</dt><dd>{selection.uploadImages === null ? "—" : selection.uploadImages ? "Bekapcsolva" : "Kikapcsolva"}</dd></div><div><dt>I/P/B egyezés</dt><dd>{selection.dualTypeMatch === null ? "—" : selection.dualTypeMatch ? "Szigorú" : "Encode kategória"}</dd></div></dl></Card>
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
  const isError = event.kind.toLowerCase().includes("error") || event.state_to === "FAILED";
  const isSuccess = event.state_to === "COMPLETED";
  return (
    <article className={isError ? "event-item event-item--error" : isSuccess ? "event-item event-item--success" : "event-item"}>
      <span className="event-item__marker">{isError ? <AlertTriangle size={15} /> : isSuccess ? <CheckCircle2 size={15} /> : <Info size={14} />}</span>
      <div><div className="event-item__heading"><strong>{event.message || event.kind}</strong><time>{formatDate(event.created_at)}</time></div>{event.state_from && event.state_to && <p>{STATE_LABELS[event.state_from]} → {STATE_LABELS[event.state_to]}</p>}{!compact && Object.keys(event.payload).length > 0 && <details><summary>Részletek</summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details>}</div>
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

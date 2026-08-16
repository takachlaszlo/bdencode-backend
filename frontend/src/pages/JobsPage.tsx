import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, CirclePlus, HardDrive, ListOrdered, Search, StopCircle, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router";
import { api, ApiError } from "../api/client";
import type { Job, JobState } from "../api/types";
import { JobCard } from "../components/JobCard";
import { Button, EmptyState, LoadingPanel, Modal, Notice, PageHeader } from "../components/ui";

const QUEUE_STATES: JobState[] = [
  "QUEUED", "SCANNING", "AWAITING_SELECTION", "READY", "ENCODING", "MUXING", "QC", "COMPARISON", "UPLOADING", "NEEDS_REVIEW", "UPLOAD_FAILED",
];
const ARCHIVE_STATES: JobState[] = ["COMPLETED", "FAILED", "CANCELLED"];

export function JobsPage({ mode }: { mode: "queue" | "archive" }) {
  const [search, setSearch] = useState("");
  const [confirmation, setConfirmation] = useState<{ action: string; job: Job } | null>(null);
  const [deleteConfirmation, setDeleteConfirmation] = useState("");
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const states = mode === "queue" ? QUEUE_STATES : ARCHIVE_STATES;
  const query = useQuery({
    queryKey: ["jobs", mode],
    queryFn: () => api.jobs(states, 500),
    refetchInterval: mode === "queue" ? 5000 : 15_000,
  });
  const filtered = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase("hu");
    if (!needle) return query.data?.items ?? [];
    return (query.data?.items ?? []).filter((job) =>
      [job.name, job.source_path, job.state].some((value) => value.toLocaleLowerCase("hu").includes(needle)),
    );
  }, [query.data, search]);
  const operation = useMutation({
    mutationFn: async ({ action, job }: { action: string; job: Job }) => {
      const revision = job.control_revision;
      if (action === "pause") return api.pauseJob(job.id, revision);
      if (action === "resume") return api.continueJob(job.id, revision);
      if (action === "cancel") return api.requestCancelJob(job.id, revision);
      if (action === "retry_failed") return api.retryJob(job.id, job.version);
      if (action === "restart_cancelled") return api.restartJob(job.id, job.version);
      if (action === "cleanup") return api.cleanupJob(job.id, job.version);
      if (action === "delete") return api.purgeJob(job.id, job.version);
      throw new Error(`Nem támogatott művelet: ${action}`);
    },
    onSuccess: (_result, variables) => {
      setConfirmation(null);
      setDeleteConfirmation("");
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ["jobs"] }),
        queryClient.invalidateQueries({ queryKey: ["job", variables.job.id] }),
        queryClient.invalidateQueries({ queryKey: ["job-storage", variables.job.id] }),
      ]);
    },
  });

  function requestAction(action: string, job: Job) {
    operation.reset();
    if (action === "prepare_release") {
      navigate(`/jobs/${encodeURIComponent(job.id)}?tab=release`);
      return;
    }
    if (action === "delete_release") {
      navigate(`/jobs/${encodeURIComponent(job.id)}?action=delete-release`);
      return;
    }
    if (["cancel", "cleanup", "delete"].includes(action)) {
      setDeleteConfirmation("");
      setConfirmation({ action, job });
      return;
    }
    operation.mutate({ action, job });
  }

  const confirmationLabel = confirmation?.action === "cancel"
    ? "Megszakítás kérése"
    : confirmation?.action === "cleanup"
      ? "Ideiglenes fájlok takarítása"
      : "Munka végleges törlése";

  return (
    <div className="page">
      <PageHeader
        eyebrow={mode === "queue" ? "Munkafolyamat" : "Előzmények"}
        title={mode === "queue" ? "Várólista" : "Elkészült munkák"}
        description={mode === "queue" ? "Az új lemezek scanje és beállítása a futó encode mellett is elkészülhet. Kódolni mindig csak az első jóváhagyott munka fog; a többi kész paraméterekkel várakozik." : "Kész, hibás és megszakított kódolások visszakereshető mellékletekkel."}
        actions={<Link className="button button--primary" to="/new"><CirclePlus size={18} /><span>Új kódolás</span></Link>}
      />

      <div className="toolbar">
        <label className="search-field">
          <Search size={18} aria-hidden="true" />
          <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Keresés név, útvonal vagy állapot alapján…" />
        </label>
        <span className="toolbar__count">{filtered.length} találat</span>
      </div>

      {query.isError && <Notice tone="danger" title="A munkák nem tölthetők be">Ellenőrizd a szerverkapcsolatot, majd próbáld újra.</Notice>}
      {operation.isError && !confirmation && <Notice tone="danger" title="A művelet sikertelen">{operation.error instanceof ApiError ? operation.error.detail : operation.error.message}</Notice>}
      {query.isLoading ? <LoadingPanel label="Munkák betöltése…" /> : filtered.length ? (
        <div className="job-grid">
          {filtered.map((job) => <JobCard key={job.id} job={job} onAction={requestAction} pendingAction={operation.isPending && operation.variables?.job.id === job.id ? operation.variables.action : null} />)}
        </div>
      ) : (
        <EmptyState
          icon={mode === "queue" ? <ListOrdered size={28} /> : <Archive size={28} />}
          title={search ? "Nincs ilyen munka" : mode === "queue" ? "A várólista üres" : "Az archívum még üres"}
          description={search ? "Próbálj más keresőkifejezést." : mode === "queue" ? "Az első forrás hozzáadásával itt jelenik meg a munkafolyamat." : "A lezárt kódolások automatikusan ide kerülnek."}
          action={!search && mode === "queue" ? <Link className="button button--secondary" to="/new">Első munka hozzáadása</Link> : undefined}
        />
      )}

      <Modal
        open={Boolean(confirmation)}
        title={confirmationLabel}
        busy={operation.isPending}
        onClose={() => { if (!operation.isPending) setConfirmation(null); }}
        footer={<><Button variant="ghost" disabled={operation.isPending} onClick={() => setConfirmation(null)}>Mégse</Button><Button variant={confirmation?.action === "delete" || confirmation?.action === "cancel" ? "danger" : "primary"} icon={confirmation?.action === "cancel" ? <StopCircle size={17} /> : confirmation?.action === "cleanup" ? <HardDrive size={17} /> : <Trash2 size={17} />} loading={operation.isPending} disabled={confirmation?.action === "delete" && deleteConfirmation !== confirmation.job.name} onClick={() => confirmation && operation.mutate(confirmation)}>{confirmationLabel}</Button></>}
      >
        {confirmation?.action === "cancel" && <Notice tone="warning">A worker rendezetten zárja le a futó folyamatot; a job csak ezután kerül Megszakítva állapotba.</Notice>}
        {confirmation?.action === "cleanup" && <Notice tone="info">Csak a sikeresen lezárt munka nagyméretű ideiglenes fájljai törlődnek. A job, a logok, a comparison és a completed release megmarad.</Notice>}
        {confirmation?.action === "delete" && <><Notice tone="danger" title="Ez nem vonható vissza">A privát jobrekord, munkaterület, logok és mellékletek törlődnek. A forrás és az elkészült release megmarad.</Notice><label className="field confirmation-field">Írd be a munka nevét:<input value={deleteConfirmation} onChange={(event) => setDeleteConfirmation(event.target.value)} /><small><code>{confirmation.job.name}</code></small></label></>}
        {operation.isError && <Notice tone="danger">{operation.error instanceof ApiError ? operation.error.detail : operation.error.message}</Notice>}
      </Modal>
    </div>
  );
}

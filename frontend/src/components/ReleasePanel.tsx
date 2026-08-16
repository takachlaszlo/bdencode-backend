import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  Clipboard,
  Download,
  FileCheck2,
  PackageCheck,
  RadioTower,
  RefreshCw,
  SearchCheck,
  ShieldCheck,
  Trash2,
  UploadCloud,
} from "lucide-react";
import { useMemo, useState } from "react";
import { api, ApiError } from "../api/client";
import type {
  Artifact,
  Job,
  ReleaseMetadataPayload,
  ReleasePreparation,
  ReleaseProfileList,
  ReleaseValidationResult,
  TrackerReleaseProfile,
} from "../api/types";
import { formatBytes, humanize } from "../utils";
import { Badge, Button, Card, EmptyState, LoadingPanel, Modal, Notice } from "./ui";

type ReleaseAction = "validate" | "build" | "export" | "dupe-check" | "seed" | "upload";
type PreparationAction = Exclude<ReleaseAction, "validate" | "export" | "upload">;

interface PreparationApprovalSnapshot {
  id: string;
  version: number;
  manifestSha256: string;
  payloadSha256: string;
  releaseName: string;
  state: string;
  preparationVersions: Record<string, number>;
}

interface ReleaseDraft {
  profileId: string;
  releaseName: string;
  title: string;
  year: string;
  edition: string;
  imdbId: string;
  tmdbId: string;
  category: string;
  sourceMedia: string;
  resolution: string;
  videoCodec: string;
  audioCodecs: string;
  languages: string;
}

const RELEASE_STATE_LABELS: Record<string, string> = {
  NOT_PREPARED: "Nincs előkészítve",
  PREPARING: "Előkészítés folyamatban",
  NEEDS_REVIEW: "Ellenőrzést kér",
  READY: "Csomag elkészült",
  SEEDING_CHECK: "Seed ellenőrzése",
  SEEDING: "qBittorrent művelet folyamatban",
  READY_TO_PUBLISH: "Publikálható",
  PUBLISHING: "Publikálás folyamatban",
  PUBLISHED: "Publikálva",
  FAILED: "Hibás",
  UNKNOWN: "Ismeretlen eredmény",
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function profilesFrom(value: ReleaseProfileList | TrackerReleaseProfile[] | undefined): TrackerReleaseProfile[] {
  if (Array.isArray(value)) return value;
  if (!value) return [];
  if (Array.isArray(value.items)) return value.items;
  return Array.isArray(value.profiles) ? value.profiles : [];
}

function preparationsFrom(value: unknown): ReleasePreparation[] {
  if (Array.isArray(value)) return value.filter(isRecord) as ReleasePreparation[];
  if (!isRecord(value)) return [];
  const items = Array.isArray(value.items)
    ? value.items
    : Array.isArray(value.preparations)
      ? value.preparations
      : [];
  return items.filter(isRecord) as ReleasePreparation[];
}

function preparationId(value: ReleasePreparation | undefined): string | null {
  if (!value) return null;
  if (typeof value.id === "string") return value.id;
  return typeof value.preparation_id === "string" ? value.preparation_id : null;
}

function preparationState(value: ReleasePreparation | undefined): string {
  if (!value) return "NOT_PREPARED";
  if (typeof value.state === "string") return value.state;
  return typeof value.status === "string" ? value.status : "UNKNOWN";
}

function releaseTone(state: string): "neutral" | "info" | "success" | "warning" | "danger" {
  if (["READY", "READY_TO_PUBLISH", "PUBLISHED"].includes(state)) return "success";
  if (["NEEDS_REVIEW", "UNKNOWN"].includes(state)) return "warning";
  if (state === "FAILED") return "danger";
  if (["PREPARING", "SEEDING_CHECK", "SEEDING", "PUBLISHING"].includes(state)) return "info";
  return "neutral";
}

function csv(value: string): string[] {
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

function selectionRecord(job: Job): Record<string, unknown> {
  return isRecord(job.selection) ? job.selection : {};
}

function outputStem(job: Job, outputArtifact?: Artifact): string {
  const source = outputArtifact?.name
    ?? outputArtifact?.path?.split(/[\\/]/).at(-1)
    ?? job.output_path?.split(/[\\/]/).at(-1)
    ?? job.name;
  return source.replace(/\.mkv$/i, "");
}

function defaultDraft(job: Job, releaseName: string): ReleaseDraft {
  const selection = selectionRecord(job);
  const yearMatch = /(?:^|[. (])(19\d{2}|20\d{2}|21\d{2})(?:[. )]|$)/.exec(releaseName);
  const tracks = Array.isArray(selection.tracks) ? selection.tracks.filter(isRecord) : [];
  const audioCodecs = [...new Set(tracks.flatMap((track) => {
    const action = typeof track.action === "string" ? track.action : "";
    return action && action !== "omit" && action !== "copy" ? [action.toUpperCase()] : [];
  }))];
  const languages = [...new Set(tracks.flatMap((track) => {
    const language = typeof track.language === "string" ? track.language : "";
    return language ? [language] : [];
  }))];
  const encoder = isRecord(selection.video) && isRecord(selection.video.settings)
    && typeof selection.video.settings.encoder === "string"
    ? selection.video.settings.encoder
    : typeof job.settings.encoder === "string"
      ? job.settings.encoder
      : "x264";
  return {
    profileId: "",
    releaseName,
    title: job.name,
    year: yearMatch?.[1] ?? String(new Date().getFullYear()),
    edition: "",
    imdbId: "",
    tmdbId: "",
    category: job.content_type === "SERIES" ? "TV" : "Movie",
    sourceMedia: job.disc_type === "UHD" ? "UHD Blu-ray" : "Blu-ray",
    resolution: job.disc_type === "UHD" ? "2160p" : "1080p",
    videoCodec: encoder === "x265" ? "H.265" : "H.264",
    audioCodecs: audioCodecs.join(", ") || "Unknown",
    languages: languages.join(", ") || "und",
  };
}

function metadataFrom(draft: ReleaseDraft): ReleaseMetadataPayload {
  return {
    schema_version: 1,
    release_name: draft.releaseName.trim(),
    title: draft.title.trim(),
    year: Number(draft.year),
    edition: draft.edition.trim() || null,
    imdb_id: draft.imdbId.trim() || null,
    tmdb_id: draft.tmdbId.trim() ? Number(draft.tmdbId) : null,
    category: draft.category.trim(),
    source_media: draft.sourceMedia.trim(),
    resolution: draft.resolution.trim(),
    video_codec: draft.videoCodec.trim(),
    audio_codecs: csv(draft.audioCodecs),
    languages: csv(draft.languages),
  };
}

function errorText(error: Error): string {
  return error instanceof ApiError ? error.detail : error.message;
}

function safeEvidence(value: unknown, key = "value"): unknown {
  if (/announce|credential|passkey|token|secret/i.test(key)) return "Rejtett érték";
  if (Array.isArray(value)) return value.map((item) => safeEvidence(item));
  if (!isRecord(value)) return value;
  return Object.fromEntries(Object.entries(value).map(([name, item]) => [name, safeEvidence(item, name)]));
}

function EvidenceList({ value, empty }: { value: unknown; empty: string }) {
  if (value == null) return <p className="muted">{empty}</p>;
  const rows = Array.isArray(value)
    ? value.slice(0, 24).map((item, index) => [String(index + 1), item] as const)
    : isRecord(value)
      ? Object.entries(value).slice(0, 24)
      : [["Állapot", value] as const];
  if (!rows.length) return <p className="muted">{empty}</p>;
  return (
    <ul className="release-check-list">
      {rows.map(([name, raw]) => {
        const item = safeEvidence(raw, name);
        const passed = item === true || (isRecord(item) && ["ok", "passed", "ready", "success"].includes(String(item.status ?? item.result).toLowerCase()));
        const failed = item === false || (isRecord(item) && ["failed", "error", "blocked"].includes(String(item.status ?? item.result).toLowerCase()));
        const display = isRecord(item) || Array.isArray(item) ? JSON.stringify(item) : String(item);
        return (
          <li key={`${name}-${display}`} className={failed ? "release-check release-check--failed" : passed ? "release-check release-check--passed" : "release-check"}>
            <span aria-hidden="true">{failed ? "!" : passed ? "✓" : "•"}</span>
            <span><strong>{humanize(name)}</strong><small>{display}</small></span>
          </li>
        );
      })}
    </ul>
  );
}

function manifestFact(manifest: Record<string, unknown>, ...keys: string[]): unknown {
  for (const key of keys) {
    const value = manifest[key];
    if (value !== undefined && value !== null && value !== "") return value;
  }
  return null;
}

function versionSnapshot(
  preparations: ReleasePreparation[],
  current: ReleasePreparation | undefined,
): Record<string, number> {
  const entries = preparations.flatMap((item) => {
    const id = preparationId(item);
    return id && typeof item.version === "number" ? [[id, item.version] as const] : [];
  });
  const currentId = preparationId(current);
  if (currentId && typeof current?.version === "number") entries.push([currentId, current.version]);
  return Object.fromEntries(entries.sort(([left], [right]) => left.localeCompare(right)));
}

function approvalSnapshot(
  current: ReleasePreparation | undefined,
  preparations: ReleasePreparation[],
): PreparationApprovalSnapshot | null {
  const id = preparationId(current);
  if (!id || typeof current?.version !== "number") return null;
  return {
    id,
    version: current.version,
    manifestSha256: typeof current.manifest_sha256 === "string" ? current.manifest_sha256 : "",
    payloadSha256: typeof current.payload_sha256 === "string" ? current.payload_sha256 : "",
    releaseName: typeof current.metadata?.release_name === "string" ? current.metadata.release_name : id,
    state: preparationState(current),
    preparationVersions: versionSnapshot(preparations, current),
  };
}

function receiptOutcome(value: unknown): string | null {
  return isRecord(value) && typeof value.outcome === "string" ? value.outcome : null;
}

function actionNeedsReview(value: ReleasePreparation | undefined): boolean {
  if (!value) return false;
  const resultState = preparationState(value);
  if (["NEEDS_REVIEW", "UNKNOWN", "FAILED"].includes(resultState)) return true;
  return [value.dupe_receipt, value.qbittorrent_receipt, value.publication_receipt]
    .some((receipt) => ["REJECTED", "UNKNOWN"].includes(receiptOutcome(receipt) ?? ""));
}

export function ReleasePanel({ job, outputArtifact }: { job: Job; outputArtifact?: Artifact }) {
  const queryClient = useQueryClient();
  const releaseName = outputStem(job, outputArtifact);
  const [draft, setDraft] = useState<ReleaseDraft>(() => defaultDraft(job, releaseName));
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [validationResult, setValidationResult] = useState<ReleaseValidationResult | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<PreparationApprovalSnapshot | null>(null);
  const [uploadTarget, setUploadTarget] = useState<PreparationApprovalSnapshot | null>(null);
  const profilesQuery = useQuery({
    queryKey: ["release-profiles"],
    queryFn: api.releaseProfiles,
    enabled: job.state === "COMPLETED",
    staleTime: 5 * 60_000,
  });
  const preparationsQuery = useQuery({
    queryKey: ["release-preparations", job.id],
    queryFn: () => api.releasePreparations(job.id),
    enabled: job.state === "COMPLETED",
    refetchInterval: 10_000,
  });
  const profiles = profilesFrom(profilesQuery.data);
  const preparations = preparationsFrom(preparationsQuery.data);
  const currentSummary = preparations.find((item) => preparationId(item) === selectedId) ?? preparations[0];
  const currentId = selectedId ?? preparationId(currentSummary);
  const detailQuery = useQuery({
    queryKey: ["release-preparation", currentId],
    queryFn: () => api.releasePreparation(currentId!),
    enabled: Boolean(currentId),
    refetchInterval: 5000,
  });
  const current = detailQuery.data ?? currentSummary;
  const state = preparationState(current);
  const manifest = {
    ...(isRecord(current?.manifest) ? current.manifest : {}),
    ...(current?.payload_path ? { payload_path: current.payload_path } : {}),
    ...(typeof current?.payload_size === "number" ? { payload_size: current.payload_size } : {}),
    ...(current?.payload_sha256 ? { payload_sha256: current.payload_sha256 } : {}),
    ...(current?.manifest_sha256 ? { manifest_sha256: current.manifest_sha256 } : {}),
    ...(current?.torrent_infohash ? { torrent_infohash: current.torrent_infohash } : {}),
    ...(current?.torrent_sha256 ? { torrent_sha256: current.torrent_sha256 } : {}),
    ...(typeof current?.kit_ready === "boolean" ? { kit_ready: current.kit_ready } : {}),
  };
  const receiptEvidence = current && [
    ["dupe_check", current.dupe_receipt],
    ["qbittorrent", current.qbittorrent_receipt],
    ["publication", current.publication_receipt],
  ].some(([, value]) => value != null)
    ? {
        dupe_check: current.dupe_receipt,
        qbittorrent: current.qbittorrent_receipt,
        publication: current.publication_receipt,
      }
    : current?.receipts;
  const preflight = validationResult ?? current?.preflight ?? current?.validation ?? receiptEvidence;
  const preview = current?.preview ?? (Object.keys(manifest).length ? manifest : null);

  const create = useMutation({
    mutationFn: () => api.createReleasePreparation(job.id, {
      profile_id: draft.profileId,
      metadata: metadataFrom({ ...draft, releaseName }),
    }),
    onSuccess: (preparation) => {
      setValidationResult(null);
      const id = preparationId(preparation);
      setSelectedId(id);
      if (id) queryClient.setQueryData(["release-preparation", id], preparation);
      void queryClient.invalidateQueries({ queryKey: ["release-preparations", job.id] });
    },
  });
  const action = useMutation({
    mutationFn: ({ name, id, version }: { name: PreparationAction; id: string; version: number }) =>
      api.releasePreparationAction(id, name, version),
    onSuccess: (preparation) => {
      setValidationResult(null);
      const id = preparationId(preparation) ?? currentId;
      if (id) queryClient.setQueryData(["release-preparation", id], preparation);
      void queryClient.invalidateQueries({ queryKey: ["release-preparations", job.id] });
    },
  });
  const validate = useMutation({
    mutationFn: ({ id, version }: { id: string; version: number }) =>
      api.validateReleasePreparation(id, version),
    onSuccess: setValidationResult,
  });
  const exportKit = useMutation({
    mutationFn: ({ id, version }: { id: string; version: number }) => api.exportReleasePreparation(id, version),
    onSuccess: ({ blob, filename }) => {
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.click();
      queueMicrotask(() => URL.revokeObjectURL(url));
    },
  });
  const upload = useMutation({
    mutationFn: ({ id, version, manifestSha256 }: { id: string; version: number; manifestSha256: string }) =>
      api.uploadReleasePreparation(id, { expected_version: version, manifest_sha256: manifestSha256 }),
    onSuccess: (preparation) => {
      const targetId = uploadTarget?.id;
      setUploadTarget(null);
      const id = preparationId(preparation) ?? targetId;
      if (id) queryClient.setQueryData(["release-preparation", id], preparation);
      void queryClient.invalidateQueries({ queryKey: ["release-preparations", job.id] });
    },
  });
  const remove = useMutation({
    mutationFn: ({ id, version }: { id: string; version: number }) => api.deleteReleasePreparation(id, version),
    onSuccess: () => {
      const removedId = deleteTarget?.id;
      setDeleteTarget(null);
      setSelectedId((selected) => selected === removedId ? null : selected);
      setValidationResult(null);
      if (removedId) queryClient.removeQueries({ queryKey: ["release-preparation", removedId] });
      void queryClient.invalidateQueries({ queryKey: ["release-preparations", job.id] });
    },
  });

  const invalidDraft = useMemo(() => {
    const year = Number(draft.year);
    return !draft.profileId || !releaseName || !draft.title.trim()
      || !Number.isInteger(year) || year < 1878 || year > 2200
      || csv(draft.audioCodecs).length === 0 || csv(draft.languages).length === 0;
  }, [draft, releaseName]);

  function run(name: ReleaseAction) {
    if (!currentId || typeof current?.version !== "number" || validate.isPending || action.isPending || exportKit.isPending || upload.isPending) return;
    if (name === "validate") {
      validate.reset();
      validate.mutate({ id: currentId, version: current.version });
      return;
    }
    if (name === "upload") {
      upload.reset();
      setUploadTarget(approvalSnapshot(current, preparations));
      return;
    }
    if (name === "export") {
      exportKit.reset();
      exportKit.mutate({ id: currentId, version: current.version });
      return;
    }
    action.reset();
    action.mutate({ name, id: currentId, version: current.version });
  }

  async function copyValue(value: unknown) {
    if (typeof value === "string" && value) await navigator.clipboard.writeText(value);
  }

  if (job.state !== "COMPLETED") {
    return <EmptyState icon={<PackageCheck size={28} />} title="A release még nem készíthető elő" description="A torrent- és feltöltési csomag csak sikeres encode és QC után építhető fel." />;
  }
  if (profilesQuery.isLoading || preparationsQuery.isLoading) return <LoadingPanel label="Release-adatok betöltése…" />;

  const profile = profiles.find((item) => item.profile_id === (current?.profile_id ?? draft.profileId));
  const description = manifestFact(manifest, "description_bbcode", "description", "bbcode");
  const payloadSize = manifestFact(manifest, "payload_size", "size_bytes");
  const manifestSha256 = typeof current?.manifest_sha256 === "string" ? current.manifest_sha256 : "";
  const hasVersion = typeof current?.version === "number";
  const busy = create.isPending || validate.isPending || action.isPending || exportKit.isPending || upload.isPending || remove.isPending;
  const canBuild = ["NOT_PREPARED", "NEEDS_REVIEW", "FAILED"].includes(state) && current?.kit_ready !== true;
  const activeState = ["PREPARING", "SEEDING_CHECK", "SEEDING", "PUBLISHING"].includes(state);
  const preservedAuditState = ["UNKNOWN", "PUBLISHED"].includes(state);
  const alreadySeeded = receiptOutcome(current?.qbittorrent_receipt) === "ADDED_AND_RECHECKING";
  const supportsDupeCheck = profile?.supports_dupe_check === true;
  const supportsQbittorrent = profile?.supports_qbittorrent === true;
  const supportsPublish = profile?.supports_publish === true;
  const actionRequiresReview = actionNeedsReview(action.data);

  return (
    <div className="release-panel">
      {(profilesQuery.isError || preparationsQuery.isError) && (
        <Notice tone="danger" title="A release-kezelő nem érhető el">
          {errorText((profilesQuery.error ?? preparationsQuery.error) as Error)}
        </Notice>
      )}
      {!profiles.length && !profilesQuery.isError && (
        <Notice tone="warning" title="Nincs használható trackerprofil">A szerveren előbb egy védett release-profilt kell engedélyezni.</Notice>
      )}

      <div className="release-layout">
        <Card className="release-form-card">
          <div className="section-heading">
            <div><span className="section-heading__icon"><PackageCheck size={19} /></span><div><h2>Release-terv</h2><p>A torrent és az upload-kit nyilvános metaadatai</p></div></div>
            {current && <Badge tone={releaseTone(state)}>{RELEASE_STATE_LABELS[state] ?? humanize(state)}</Badge>}
          </div>

          {preparations.length > 0 && (
            <label className="field">
              Korábbi előkészítés
              <select value={currentId ?? ""} onChange={(event) => { setSelectedId(event.target.value); setValidationResult(null); }}>
                {preparations.map((item, index) => {
                  const id = preparationId(item);
                  return id ? <option value={id} key={id}>{item.metadata?.release_name ?? `Előkészítés ${index + 1}`} · {RELEASE_STATE_LABELS[preparationState(item)] ?? preparationState(item)}</option> : null;
                })}
              </select>
            </label>
          )}

          <form onSubmit={(event) => { event.preventDefault(); create.mutate(); }}>
            <div className="release-form-grid">
              <label className="field">Trackerprofil<select required value={draft.profileId} onChange={(event) => setDraft((value) => ({ ...value, profileId: event.target.value }))}><option value="">Válassz profilt…</option>{profiles.map((item) => <option key={item.profile_id} value={item.profile_id}>{item.display_name}</option>)}</select></label>
              <label className="field">Év<input required inputMode="numeric" min="1878" max="2200" type="number" value={draft.year} onChange={(event) => setDraft((value) => ({ ...value, year: event.target.value }))} /></label>
              <label className="field release-field--wide">Release-név<input required readOnly aria-readonly="true" maxLength={240} value={releaseName} /><small>Az ellenőrzött OUTPUT MKV fájlnevéből származik.</small></label>
              <label className="field release-field--wide">Cím<input required maxLength={300} value={draft.title} onChange={(event) => setDraft((value) => ({ ...value, title: event.target.value }))} /></label>
              <label className="field">Edition<input maxLength={160} value={draft.edition} onChange={(event) => setDraft((value) => ({ ...value, edition: event.target.value }))} /></label>
              <label className="field">Kategória<input required value={draft.category} onChange={(event) => setDraft((value) => ({ ...value, category: event.target.value }))} /></label>
              <label className="field">IMDb ID<input pattern="tt[0-9]{7,10}" placeholder="tt1234567" value={draft.imdbId} onChange={(event) => setDraft((value) => ({ ...value, imdbId: event.target.value }))} /></label>
              <label className="field">TMDb ID<input min="1" type="number" value={draft.tmdbId} onChange={(event) => setDraft((value) => ({ ...value, tmdbId: event.target.value }))} /></label>
              <label className="field">Forrás<input required value={draft.sourceMedia} onChange={(event) => setDraft((value) => ({ ...value, sourceMedia: event.target.value }))} /></label>
              <label className="field">Felbontás<input required value={draft.resolution} onChange={(event) => setDraft((value) => ({ ...value, resolution: event.target.value }))} /></label>
              <label className="field">Videokodek<input required value={draft.videoCodec} onChange={(event) => setDraft((value) => ({ ...value, videoCodec: event.target.value }))} /></label>
              <label className="field">Audiókodekek<input required value={draft.audioCodecs} onChange={(event) => setDraft((value) => ({ ...value, audioCodecs: event.target.value }))} /><small>Vesszővel elválasztva.</small></label>
              <label className="field release-field--wide">Nyelvek<input required value={draft.languages} onChange={(event) => setDraft((value) => ({ ...value, languages: event.target.value }))} /><small>Normalizált BCP-47 kódok, vesszővel elválasztva.</small></label>
            </div>
            <Button className="release-create-button" type="submit" icon={<PackageCheck size={17} />} loading={create.isPending} disabled={invalidDraft || !profiles.length}>Új előkészítés létrehozása</Button>
          </form>
          {create.isError && <Notice tone="danger" title="Az előkészítés nem hozható létre">{errorText(create.error)}</Notice>}
        </Card>

        <div className="release-side-stack">
          <Card className="release-status-card">
            <span className="eyebrow">Kiválasztott terv</span>
            <h2>{current?.metadata?.release_name ?? releaseName}</h2>
            <dl className="summary-list summary-list--stacked">
              <div><dt>Állapot</dt><dd>{RELEASE_STATE_LABELS[state] ?? humanize(state)}</dd></div>
              <div><dt>Tracker</dt><dd>{profile?.display_name ?? current?.profile_id ?? "—"}</dd></div>
              <div><dt>Payload</dt><dd>{String(manifestFact(manifest, "payload_path", "release_name") ?? "—")}</dd></div>
              <div><dt>Méret</dt><dd>{typeof payloadSize === "number" ? formatBytes(payloadSize) : "—"}</dd></div>
              <div><dt>Infohash</dt><dd><code>{String(manifestFact(manifest, "torrent_infohash", "infohash") ?? "—")}</code></dd></div>
            </dl>
          </Card>
          <Card className="release-payload-card">
            <span className="eyebrow">Torrent payload</span><h2>Egy ellenőrzött MKV</h2>
            <p>A comparison, analysis és tulajdonosi rekord nem kerül automatikusan a torrentbe.</p>
            <div className="release-payload-path"><FileCheck2 size={18} /><code>{String(manifestFact(manifest, "payload_path") ?? `${releaseName}/${releaseName}.mkv`)}</code></div>
          </Card>
        </div>
      </div>

      {current && (
        <>
          <div className="release-evidence-grid">
            <Card className="release-evidence-card"><div className="section-heading"><div><span className="section-heading__icon"><ShieldCheck size={18} /></span><div><h2>Preflight</h2><p>Blokkoló és tájékoztató ellenőrzések</p></div></div></div><EvidenceList value={preflight} empty="A validáció még nem futott le." /></Card>
            <Card className="release-evidence-card"><div className="section-heading"><div><span className="section-heading__icon"><SearchCheck size={18} /></span><div><h2>Csomag-előnézet</h2><p>Hash-pinnelt payload és publikus sidecarok</p></div></div></div><EvidenceList value={preview} empty="A csomag még nem épült fel." /></Card>
          </div>

          <Card className="release-actions-card" aria-busy={busy}>
            <div><span className="eyebrow">Műveletek</span><h2>Validálás, export és publikálás</h2><p>A trackerfeltöltés külön megerősítést igényel; bizonytalan eredmény nem indul újra automatikusan.</p></div>
            <div className="release-actions">
              <Button variant="secondary" icon={<ShieldCheck size={16} />} loading={validate.isPending} disabled={busy || !hasVersion} onClick={() => run("validate")}>Validálás</Button>
              <Button icon={<PackageCheck size={16} />} loading={action.isPending && action.variables?.name === "build"} disabled={busy || !hasVersion || !canBuild} onClick={() => run("build")}>Csomag építése</Button>
              <Button title={!supportsDupeCheck ? "A trackerprofil nem támogat dupe checket." : undefined} variant="secondary" icon={<SearchCheck size={16} />} loading={action.isPending && action.variables?.name === "dupe-check"} disabled={busy || !hasVersion || state !== "READY" || !supportsDupeCheck} onClick={() => run("dupe-check")}>Dupe check</Button>
              <Button variant="secondary" icon={<Download size={16} />} loading={exportKit.isPending} disabled={busy || !hasVersion || current?.kit_ready !== true} onClick={() => run("export")}>Torrent export</Button>
              <Button title={!supportsQbittorrent ? "A trackerprofilhoz nincs qBittorrent-integráció." : alreadySeeded ? "A torrent már hozzá lett adva és teljes újraellenőrzés alatt áll." : undefined} variant="secondary" icon={<RadioTower size={16} />} loading={action.isPending && action.variables?.name === "seed"} disabled={busy || !hasVersion || !["READY", "READY_TO_PUBLISH"].includes(state) || !supportsQbittorrent || alreadySeeded} onClick={() => run("seed")}>Seed előkészítése</Button>
              <Button title={!supportsPublish ? "A trackerprofil nem támogat közvetlen feltöltést." : undefined} icon={<UploadCloud size={16} />} disabled={busy || !hasVersion || !manifestSha256 || state !== "READY_TO_PUBLISH" || !supportsPublish} onClick={() => run("upload")}>Trackerfeltöltés</Button>
              {typeof description === "string" && <Button variant="ghost" icon={<Clipboard size={16} />} disabled={busy} onClick={() => void copyValue(description)}>Leírás másolása</Button>}
              <Button variant="danger" icon={<Trash2 size={16} />} disabled={busy || !hasVersion || activeState || preservedAuditState} onClick={() => setDeleteTarget(approvalSnapshot(current, preparations))}>Terv törlése</Button>
            </div>
          </Card>
          {validate.isError && <Notice tone="danger" title="A validáció sikertelen">{errorText(validate.error)}</Notice>}
          {action.isError && <Notice tone="danger" title="A release-művelet sikertelen">{errorText(action.error)}</Notice>}
          {exportKit.isError && <Notice tone="danger" title="Az export sikertelen">{errorText(exportKit.error)}</Notice>}
          {upload.isError && uploadTarget === null && <Notice tone="danger" title="A trackerfeltöltés sikertelen">{errorText(upload.error)}</Notice>}
          {current?.error && <Notice tone="danger" title="Tartós release-hiba">{current.error}</Notice>}
          {validate.isSuccess && <Notice tone={validationResult?.valid ? "success" : "warning"} title={validationResult?.valid ? "A preflight sikeres" : "A preflight javítást kér"}><CheckCircle2 size={16} /> {validationResult?.valid ? "A payload, a trackerprofil és a bizonyítékok érvényesek." : `${validationResult?.failures.length ?? 0} blokkoló eltérés található.`}</Notice>}
          {action.isSuccess && <Notice tone={actionRequiresReview ? "warning" : "success"} title={actionRequiresReview ? "A release-művelet ellenőrzést kér" : "A release-művelet elkészült"}><CheckCircle2 size={16} /> {actionRequiresReview ? "A művelet lezárult, de a receipt vagy az új állapot operátori ellenőrzést igényel." : "A friss állapot és bizonyítékok betöltve."}</Notice>}
          {detailQuery.isError && <Notice tone="warning" title="A részletes állapot nem frissíthető"><Button variant="ghost" icon={<RefreshCw size={15} />} onClick={() => void detailQuery.refetch()}>Újrapróbálás</Button></Notice>}
        </>
      )}

      <Modal open={deleteTarget !== null} title="Törlöd ezt az előkészítést?" busy={remove.isPending} onClose={() => { if (!remove.isPending) setDeleteTarget(null); }} footer={<><Button variant="ghost" disabled={remove.isPending} onClick={() => setDeleteTarget(null)}>Mégse</Button><Button variant="danger" icon={<Trash2 size={17} />} loading={remove.isPending} disabled={!deleteTarget} onClick={() => deleteTarget && remove.mutate({ id: deleteTarget.id, version: deleteTarget.version })}>Előkészítés törlése</Button></>}>
        <Notice tone="warning">A release-terv, torrent és upload-kit revíziója törlődik. Az elkészült MKV-hoz és a jobhoz a rendszer nem nyúl.</Notice>
        {deleteTarget && <dl className="summary-list summary-list--stacked"><div><dt>Rögzített terv</dt><dd>{deleteTarget.releaseName}</dd></div><div><dt>Állapot / revízió</dt><dd>{RELEASE_STATE_LABELS[deleteTarget.state] ?? humanize(deleteTarget.state)} · v{deleteTarget.version}</dd></div><div><dt>Manifest SHA-256</dt><dd><code>{deleteTarget.manifestSha256 || "—"}</code></dd></div><div><dt>Payload SHA-256</dt><dd><code>{deleteTarget.payloadSha256 || "—"}</code></dd></div><div><dt>Előkészítés-revíziók</dt><dd><code>{JSON.stringify(deleteTarget.preparationVersions)}</code></dd></div></dl>}
        {remove.isError && <Notice tone="danger">{errorText(remove.error)}</Notice>}
      </Modal>
      <Modal open={uploadTarget !== null} title="Publikálod a release-t?" busy={upload.isPending} onClose={() => { if (!upload.isPending) setUploadTarget(null); }} footer={<><Button variant="ghost" disabled={upload.isPending} onClick={() => setUploadTarget(null)}>Mégse</Button><Button icon={<UploadCloud size={17} />} loading={upload.isPending} disabled={!uploadTarget?.manifestSha256} onClick={() => uploadTarget && upload.mutate({ id: uploadTarget.id, version: uploadTarget.version, manifestSha256: uploadTarget.manifestSha256 })}>Trackerfeltöltés indítása</Button></>}>
        <Notice tone="warning" title="Ez külső művelet">A jóváhagyott manifest kerül a beállított trackerre. Bizonytalan hálózati eredménynél a rendszer nem próbálkozik automatikusan újra.</Notice>
        {uploadTarget && <dl className="summary-list summary-list--stacked"><div><dt>Rögzített terv</dt><dd>{uploadTarget.releaseName}</dd></div><div><dt>Állapot / revízió</dt><dd>{RELEASE_STATE_LABELS[uploadTarget.state] ?? humanize(uploadTarget.state)} · v{uploadTarget.version}</dd></div><div><dt>Manifest SHA-256</dt><dd><code>{uploadTarget.manifestSha256}</code></dd></div><div><dt>Előkészítés-revíziók</dt><dd><code>{JSON.stringify(uploadTarget.preparationVersions)}</code></dd></div></dl>}
        <label className="field">Jóváhagyó operátor<input readOnly aria-readonly="true" value="hitelesített proxy-operátor" /><small>Az identitást a megbízható reverse proxy adja át; a felületen nem módosítható.</small></label>
        {upload.isError && <Notice tone="danger">{errorText(upload.error)}</Notice>}
      </Modal>
    </div>
  );
}

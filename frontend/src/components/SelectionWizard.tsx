import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Check,
  ChevronDown,
  Clapperboard,
  Copy,
  Film,
  Languages,
  ListVideo,
  Music,
  Palette,
  ScanLine,
  Search,
  Settings2,
  SlidersHorizontal,
  Sparkles,
  Subtitles,
  WandSparkles,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { Dispatch, ReactNode, SetStateAction } from "react";
import type {
  DetailLevel,
  DiscScanResult,
  FieldSpec,
  ImageUploadProvider,
  Job,
  MediaStream,
  Playlist,
  SelectionPayload,
  SelectionValidation,
  TrackAction,
  TrackSelection,
} from "../api/types";
import { api, ApiError } from "../api/client";
import {
  blockingSourceColorFields,
  hasSafeSourceColorRecommendation,
  missingSourceColorFields,
  parseSourceColor,
  SOURCE_COLOR_FIELD_LABELS,
  sourceColorIssueFromPayload,
  suggestedSourceColor,
} from "../colorMetadata";
import type { SourceColorField, SourceColorMetadata } from "../colorMetadata";
import { normalizeStoredSelection } from "../selection";
import type { StoredTrackSelection } from "../selection";
import { basename, formatDuration, humanize, suggestedOutputName } from "../utils";
import { Badge, Button, Card, Notice, ProgressBar } from "./ui";

const GROUP_LABELS: Record<string, string> = {
  rate_control: "Minőség és sebesség",
  format: "Formátum és színtér",
  gop: "GOP és képtípusok",
  motion: "Mozgásbecslés",
  psychovisual: "Pszichovizuális finomhangolás",
  filter: "Képszűrés",
  hdr: "HDR10",
  bitstream: "Bitstream",
  x265: "x265-specifikus",
};

const IMAGE_UPLOAD_PROVIDER_LABELS: Record<ImageUploadProvider, string> = {
  auto: "Automatikus: ImgBB → Catbox → Freeimage",
  imgbb: "Csak ImgBB",
  catbox: "Csak Catbox",
  freeimage: "Csak Freeimage",
};

const AUDIO_TRACK_ACTIONS: TrackAction[] = ["copy", "flac", "ac3", "eac3", "dts", "omit"];
const AUDIO_ACTION_DETAILS: Record<TrackAction, { label: string; description: string }> = {
  copy: { label: "Copy", description: "Az eredeti hangsáv változtatás nélkül" },
  flac: { label: "FLAC", description: "Veszteségmentes PCM-konverzió, eredeti csatornaszám" },
  ac3: { label: "AC-3", description: "640 kb/s · 48 kHz · legfeljebb 5.1" },
  eac3: { label: "E-AC-3", description: "1024 kb/s · 48 kHz · legfeljebb 5.1" },
  dts: { label: "DTS", description: "DTS core · 1536 kb/s · 48 kHz · legfeljebb 5.1" },
  omit: { label: "Kihagyás", description: "A sáv nem kerül a kész MKV-ba" },
};

const AUDIO_TRANSCODE_ACTIONS = new Set<TrackAction>(["flac", "ac3", "eac3", "dts"]);

function imageUploadProvider(value: unknown): ImageUploadProvider {
  return value === "imgbb" || value === "catbox" || value === "freeimage"
    ? value
    : "auto";
}

const FIELD_LABELS: Record<string, string> = {
  encoder: "Kódoló",
  crf: "CRF minőség",
  preset: "Preset",
  tune: "Tartalmi hangolás",
  profile: "Profil",
  level: "Dekóderszint",
  bit_depth: "Bitmélység",
  pixel_format: "Pixelformátum",
  color: "Színtér",
  vbv: "VBV korlátozás",
  keyint: "Maximális GOP-hossz",
  min_keyint: "Minimális GOP-hossz",
  scenecut: "Jelenetváltás-érzékenység",
  open_gop: "Nyitott GOP",
  bframes: "B-framek száma",
  b_adapt: "Adaptív B-frame",
  b_pyramid: "B-piramis",
  ref: "Referenciaképek",
  rc_lookahead: "Előretekintés",
  weightp: "Súlyozott P-predikció",
  weightb: "Súlyozott B-predikció",
  me: "Mozgásbecslési mód",
  merange: "Keresési tartomány",
  subme: "Részpixeles finomság",
  trellis: "Trellis",
  partitions: "Partíciók",
  direct: "Direkt predikció",
  aq_mode: "AQ mód",
  aq_strength: "AQ erősség",
  qcomp: "Kvantálási görbe",
  psy_rd: "Psy-RD",
  psy_rdoq: "Psy-RDOQ",
  deblock_alpha: "Deblock alpha",
  deblock_beta: "Deblock beta",
  chroma_qp_offset: "Chroma QP eltérés",
  sao: "SAO",
  limit_sao: "Korlátozott SAO",
  strong_intra_smoothing: "Erős intra simítás",
  rect: "Négyszögletes partíciók",
  amp: "Aszimmetrikus partíciók",
  early_skip: "Korai skip",
  rskip: "Rekurzív skip",
  aud: "AUD NAL egységek",
  repeat_headers: "Fejlécek ismétlése",
  annexb: "Annex B",
};

const FIELD_HELP: Record<string, string> = {
  crf: "Alacsonyabb érték: jobb kép és nagyobb fájl. A javaslat jó kiindulópont.",
  preset: "Lassabb preset általában jobb tömörítést ad, de jelentősen tovább tart.",
  tune: "A kép jellegéhez igazítja a pszichovizuális döntéseket.",
  bframes: "Legalább 1 kötelező az I/P/B összehasonlítás miatt.",
  aq_strength: "A részletgazdag és sötét területek bitelosztását szabályozza.",
  ref: "Több referenciakép javíthatja a tömörítést, de lassabb és memóriaigényesebb.",
  rc_lookahead: "Több jövőbeli képkocka elemzése jobb döntéseket, de nagyobb memóriaigényt jelent.",
};

const LOCKED_FIELDS = new Set(["encoder", "profile", "bit_depth", "pixel_format", "color", "hdr10"]);

function detectedLanguage(stream: MediaStream): string | null {
  return stream.language?.iso639_2t || stream.language?.bcp47 || null;
}

function initialTrackSelections(playlist: Playlist): TrackSelection[] {
  const media = playlist.streams.filter((stream) => stream.kind !== "video");
  const firstAudio = media.find((stream) => stream.kind === "audio")?.id;
  return media.map((stream, index) => {
    const keep = stream.kind === "audio"
      ? stream.default || stream.id === firstAudio
      : stream.forced || stream.default;
    return {
      stream_id: stream.id,
      action: keep ? "copy" : "omit",
      language: detectedLanguage(stream),
      name: stream.title,
      default: stream.default,
      forced: stream.forced,
      order: index,
    };
  });
}

function mergeTrackSelections(playlist: Playlist, saved: StoredTrackSelection[] = []): TrackSelection[] {
  const savedById = new Map(saved.map((selection) => [selection.stream_id, selection]));
  return initialTrackSelections(playlist).map((fallback, order) => {
    const existing = savedById.get(fallback.stream_id);
    return existing
      ? { ...fallback, ...existing, stream_id: fallback.stream_id, order }
      : fallback;
  });
}

function normalizeDetailLevel(value: unknown): DetailLevel {
  return value === "advanced" || value === "pro" || value === "beginner" ? value : "beginner";
}

function editableRecommendation(
  fields: FieldSpec[],
  recommendation: Record<string, unknown>,
): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const field of fields) {
    if (LOCKED_FIELDS.has(field.name) || field.name === "vbv") continue;
    if (field.name in recommendation) result[field.name] = recommendation[field.name];
  }
  return result;
}

function suggestedTemporalFilter(playlist: Playlist): string {
  const order = playlist.streams.find((stream) => stream.kind === "video")?.video?.field_order;
  if (!order || order === "progressive" || order === "unknown") return "progressive";
  return order.toLowerCase().includes("bb") || order.toLowerCase().includes("bottom") ? "bwdif_bff" : "bwdif_tff";
}

function fieldLabel(name: string): string {
  return FIELD_LABELS[name] || humanize(name);
}

export function SelectionWizard({
  job,
  scan,
  onComplete,
}: {
  job: Job;
  scan: DiscScanResult;
  onComplete: () => void;
}) {
  const initial = normalizeStoredSelection(job.selection);
  const initialPlaylist = initial?.playlistId
    ? scan.playlists.find((item) => item.playlist_id === initial.playlistId)
    : undefined;
  const defaultPlaylist = initialPlaylist
    ?? scan.playlists.find((item) => item.recommended)
    ?? scan.playlists[0];
  const [step, setStep] = useState(1);
  const [playlistId, setPlaylistId] = useState(defaultPlaylist?.playlist_id ?? "");
  const [angle, setAngle] = useState(
    initialPlaylist ? Math.min(initial?.angle ?? 1, initialPlaylist.angle_count) : 1,
  );
  const [tracks, setTracks] = useState<TrackSelection[]>(
    defaultPlaylist ? mergeTrackSelections(defaultPlaylist, initialPlaylist ? initial?.tracks : []) : [],
  );
  const initialDetail = normalizeDetailLevel(initial?.detailLevel ?? job.settings.detail_level);
  const [detailLevel, setDetailLevel] = useState<DetailLevel>(initialDetail);
  const [temporalFilter, setTemporalFilter] = useState(
    initial?.temporalFilter ?? (defaultPlaylist ? suggestedTemporalFilter(defaultPlaylist) : "progressive"),
  );
  const [crop, setCrop] = useState(initial?.crop ?? { left: 0, top: 0, right: 0, bottom: 0 });
  const encoder = scan.disc_kind === "uhd" ? "x265" : "x264";
  const [outputName, setOutputName] = useState(
    initial?.outputName ?? suggestedOutputName(job.name || basename(scan.source), encoder),
  );
  const [uploadImages, setUploadImages] = useState(
    initial?.uploadImages ?? Boolean(job.settings.upload_images ?? true),
  );
  const [selectedImageProvider, setSelectedImageProvider] = useState<ImageUploadProvider>(
    initial?.imageUploadProvider ?? imageUploadProvider(job.settings.image_upload_provider),
  );
  const [settings, setSettings] = useState<Record<string, unknown>>(initial?.settings ?? {});
  const [settingsSearch, setSettingsSearch] = useState("");
  const [validation, setValidation] = useState<SelectionValidation | null>(null);
  const initialVideo = defaultPlaylist?.streams.find((stream) => stream.kind === "video")?.video;
  const initialConfirmedColor = parseSourceColor(initial?.settings.color);
  const [colorDraft, setColorDraft] = useState<SourceColorMetadata>(
    initialConfirmedColor ?? suggestedSourceColor(initialVideo, scan.disc_kind),
  );
  const [colorConfirmed, setColorConfirmed] = useState(Boolean(initialConfirmedColor));
  const initializedRecommendation = useRef(
    initial && Object.keys(initial.settings).length > 0 ? `${encoder}:${detailLevel}` : "",
  );
  const queryClient = useQueryClient();

  const playlist = scan.playlists.find((item) => item.playlist_id === playlistId) ?? scan.playlists[0];
  const schema = useQuery({
    queryKey: ["profile-schema", encoder, detailLevel],
    queryFn: () => api.profileSchema(encoder, detailLevel),
  });
  const recommendation = useQuery({
    queryKey: ["profile-recommendation", encoder, detailLevel, job.content_type],
    queryFn: () => api.profileRecommendation(encoder, detailLevel, job.content_type),
  });

  useEffect(() => {
    const key = `${encoder}:${detailLevel}`;
    if (!schema.data || !recommendation.data || initializedRecommendation.current === key) return;
    setSettings((current) => ({
      ...editableRecommendation(schema.data.fields, recommendation.data.settings),
      ...(parseSourceColor(current.color) ? { color: current.color } : {}),
    }));
    initializedRecommendation.current = key;
  }, [detailLevel, encoder, recommendation.data, schema.data]);

  const payload = useMemo<SelectionPayload>(() => ({
    playlist_id: playlistId,
    angle,
    output_name: outputName.trim().replace(/\.mkv$/i, ""),
    video: {
      detail_level: detailLevel,
      temporal_filter: temporalFilter,
      crop,
      settings,
    },
    tracks,
    upload_images: uploadImages,
    image_upload_provider: selectedImageProvider,
    dual_type_match: true,
  }), [angle, crop, detailLevel, outputName, playlistId, selectedImageProvider, settings, temporalFilter, tracks, uploadImages]);

  const validate = useMutation({
    mutationFn: () => api.validateSelection(job.id, payload, job.version),
    onSuccess: (result) => setValidation(result),
  });
  const save = useMutation({
    mutationFn: () => api.saveSelection(job.id, payload, job.version),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["job", job.id] });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      onComplete();
    },
  });

  function clearPlanFeedback() {
    setValidation(null);
    validate.reset();
    save.reset();
  }

  function confirmSourceColor() {
    setSettings((current) => ({ ...current, color: colorDraft }));
    setColorConfirmed(true);
    clearPlanFeedback();
  }

  function choosePlaylist(id: string) {
    const selected = scan.playlists.find((item) => item.playlist_id === id);
    if (!selected) return;
    setPlaylistId(id);
    setAngle(1);
    setTracks(initialTrackSelections(selected));
    setTemporalFilter(suggestedTemporalFilter(selected));
    const selectedVideo = selected.streams.find((stream) => stream.kind === "video")?.video;
    setColorDraft(suggestedSourceColor(selectedVideo, scan.disc_kind));
    setColorConfirmed(false);
    setSettings((current) => {
      const next = { ...current };
      delete next.color;
      return next;
    });
    clearPlanFeedback();
  }

  function updateTrack(streamId: string, update: Partial<TrackSelection>) {
    setTracks((current) => current.map((item) => item.stream_id === streamId ? { ...item, ...update } : item));
    setValidation(null);
  }

  function updateSetting(field: FieldSpec, raw: string | boolean) {
    const numeric = field.value_type === "integer" || field.value_type === "number";
    if (numeric && String(raw).trim() === "") {
      setSettings((current) => {
        const next = { ...current };
        delete next[field.name];
        return next;
      });
      setValidation(null);
      return;
    }
    const value = field.value_type === "boolean"
      ? raw
      : field.value_type === "integer"
        ? Number.parseInt(String(raw), 10)
        : field.value_type === "number"
          ? Number.parseFloat(String(raw))
          : raw;
    if (numeric && typeof value === "number" && !Number.isFinite(value)) return;
    setSettings((current) => ({ ...current, [field.name]: value }));
    setValidation(null);
  }

  function updateSettings(next: SetStateAction<Record<string, unknown>>) {
    setSettings(next);
    setValidation(null);
  }

  const retainedTracks = tracks.filter((item) => item.action !== "omit");
  const unresolvedTracks = retainedTracks.filter((item) => !item.language);
  const videoStream = playlist?.streams.find((stream) => stream.kind === "video");
  const missingColorFields = missingSourceColorFields(videoStream?.video);
  const colorApiIssue = validate.error instanceof ApiError
    ? sourceColorIssueFromPayload(validate.error.payload)
    : null;
  const needsColorConfirmation = blockingSourceColorFields(videoStream?.video).length > 0
    || Boolean(colorApiIssue);
  const safeColorRecommendation = hasSafeSourceColorRecommendation(videoStream?.video, scan.disc_kind);
  const reportedMissingColorFields = colorApiIssue?.missing.length
    ? colorApiIssue.missing
    : missingColorFields;
  const sourceInterlaced = videoStream?.video?.field_order && !["progressive", "unknown"].includes(videoStream.video.field_order);
  const expectedTrackIds = playlist?.streams.filter((stream) => stream.kind !== "video").map((stream) => stream.id) ?? [];
  const selectedTrackIds = new Set(tracks.map((track) => track.stream_id));
  const hasCompleteTrackPlan = expectedTrackIds.every((streamId) => selectedTrackIds.has(streamId));
  const canNext = step === 1 ? Boolean(playlist) : step === 2 ? hasCompleteTrackPlan : step === 3 ? Boolean(outputName.trim()) : true;

  return (
    <div className="selection-wizard">
      <div className="selection-wizard__header">
        <div>
          <span className="eyebrow">Scan elkészült</span>
          <h2>Kódolási beállítások</h2>
          <p>Jóváhagyás után a worker automatikusan elindítja a szerveroldalon ellenőrzött tervet; külön indítógomb nincs.</p>
        </div>
        <div className="codec-lockup">
          <span>{scan.disc_kind === "uhd" ? "UHD" : "BD"}</span>
          <strong>{encoder}</strong>
          <small>{scan.disc_kind === "uhd" ? "HDR10 megtartással" : "SDR Blu-ray"}</small>
        </div>
      </div>

      <div className="wizard-steps wizard-steps--four">
        {["Playlist", "Sávok", "Videó", "Ellenőrzés"].map((label, index) => (
          <button
            type="button"
            key={label}
            className={index + 1 === step ? "wizard-step wizard-step--active" : index + 1 < step ? "wizard-step wizard-step--complete" : "wizard-step"}
            onClick={() => index + 1 < step && setStep(index + 1)}
            disabled={index + 1 > step}
            aria-current={index + 1 === step ? "step" : undefined}
          >
            <span>{index + 1 < step ? <Check size={15} /> : index + 1}</span>{label}
          </button>
        ))}
      </div>

      {scan.warnings.length > 0 && (
        <Notice tone="warning" title="A scan figyelmeztetései">
          <ul>{scan.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
        </Notice>
      )}

      {step === 1 && (
        <div className="playlist-grid">
          {scan.playlists.map((item) => {
            const video = item.streams.find((stream) => stream.kind === "video")?.video;
            return (
              <button type="button" key={item.playlist_id} className={playlistId === item.playlist_id ? "playlist-card playlist-card--selected" : "playlist-card"} aria-pressed={playlistId === item.playlist_id} onClick={() => choosePlaylist(item.playlist_id)}>
                <span className="playlist-card__visual"><Film size={27} aria-hidden="true" /><small>{video?.width ?? "?"}×{video?.height ?? "?"}</small></span>
                <span className="playlist-card__content">
                  <span className="playlist-card__top">
                    <strong>{item.edition_label || (item.episode_number ? `${item.episode_number}. epizód` : `Playlist ${item.playlist_id}`)}</strong>
                    {item.recommended && <Badge tone="success">Ajánlott</Badge>}
                  </span>
                  <span className="playlist-card__facts">
                    <span>{formatDuration(item.duration_seconds)}</span>
                    <span>{item.chapters.length} fejezet</span>
                    <span>{item.segments.length} szegmens</span>
                    <span>{item.angle_count} szög</span>
                  </span>
                  <span className="playlist-card__tags">
                    <Badge>{video?.codec?.toUpperCase() || "VIDEÓ"}</Badge>
                    {video?.hdr10 && <Badge tone="info">HDR10</Badge>}
                    {video?.dolby_vision && <Badge tone="warning">DV → HDR10</Badge>}
                    {item.seamless_branching && <Badge>Seamless branching</Badge>}
                  </span>
                </span>
                <span className="playlist-card__check">{playlistId === item.playlist_id && <Check size={16} />}</span>
              </button>
            );
          })}
          {playlist && playlist.angle_count > 1 && (
            <label className="field playlist-angle-field">
              <span>Kameraállás / szög</span>
              <select value={angle} onChange={(event) => { setAngle(Number(event.target.value)); setValidation(null); }}>
                {Array.from({ length: playlist.angle_count }, (_, index) => index + 1).map((value) => (
                  <option key={value} value={value}>{value}. szög</option>
                ))}
              </select>
              <small>A kiválasztott playlist több Blu-ray szöget tartalmaz; válaszd ki a feldolgozandót.</small>
            </label>
          )}
        </div>
      )}

      {step === 2 && playlist && (
        <div className="track-sections">
          <TrackTable
            title="Hangsávok"
            icon={<Music size={20} />}
            streams={playlist.streams.filter((stream) => stream.kind === "audio")}
            selections={tracks}
            onUpdate={updateTrack}
          />
          <TrackTable
            title="Feliratok"
            icon={<Subtitles size={20} />}
            streams={playlist.streams.filter((stream) => stream.kind === "subtitle")}
            selections={tracks}
            onUpdate={updateTrack}
          />
          {unresolvedTracks.length > 0 && (
            <Notice tone="warning" title="Hiányzó nyelv">
              {unresolvedTracks.length} megtartott sáv nyelve bizonytalan. Megadhatod most, vagy a hangot a worker beszédmintákból próbálja azonosítani; PGS feliratnál kézi megadás szükséges.
            </Notice>
          )}
        </div>
      )}

      {step === 3 && playlist && (
        <div className="video-settings-layout">
          <div className="video-settings-main">
            {needsColorConfirmation && (
              <SourceColorConfirmation
                video={videoStream?.video}
                discKind={scan.disc_kind}
                missing={reportedMissingColorFields}
                value={colorDraft}
                confirmed={colorConfirmed}
                safeRecommendation={safeColorRecommendation}
                onChange={(next) => {
                  setColorDraft(next);
                  setColorConfirmed(false);
                  setSettings((current) => {
                    const updated = { ...current };
                    delete updated.color;
                    return updated;
                  });
                  clearPlanFeedback();
                }}
                onConfirm={confirmSourceColor}
              />
            )}
            <Card className="settings-card">
              <div className="section-heading">
                <div><span className="section-heading__icon"><WandSparkles size={19} /></span><div><h3>Ajánlott profil</h3><p>A scan és a tartalomtípus alapján</p></div></div>
                <div className="detail-switch" role="group" aria-label="Profil részletessége">
                  {(["beginner", "advanced", "pro"] as DetailLevel[]).map((level) => (
                    <button type="button" key={level} className={detailLevel === level ? "active" : ""} aria-pressed={detailLevel === level} onClick={() => { setDetailLevel(level); setValidation(null); }}>
                      {level === "beginner" ? "Kezdő" : level === "advanced" ? "Haladó" : "Profi"}
                    </button>
                  ))}
                </div>
              </div>
              {schema.isError || recommendation.isError ? (
                <Notice tone="danger">A profil sémája vagy ajánlása nem tölthető be. Próbáld újra az oldal frissítése után.</Notice>
              ) : schema.isLoading || recommendation.isLoading ? <ProgressBar value={0.45} label="Profil betöltése…" /> : (
                <ProfileFields
                  fields={schema.data?.fields ?? []}
                  settings={settings}
                  recommendation={recommendation.data?.settings ?? {}}
                  search={settingsSearch}
                  onSearch={setSettingsSearch}
                  onUpdate={updateSetting}
                  onSettings={updateSettings}
                />
              )}
            </Card>

            <Card className="settings-card">
              <div className="section-heading">
                <div><span className="section-heading__icon"><ScanLine size={19} /></span><div><h3>Képkocka-kezelés és crop</h3><p>{videoStream?.video?.width}×{videoStream?.video?.height} · {videoStream?.video?.field_order || "ismeretlen mezősorrend"}</p></div></div>
              </div>
              {sourceInterlaced && <Notice tone="warning">A scan váltottsoros forrást jelzett. Ellenőrizd, hogy IVTC vagy deinterlace szükséges-e; ezt nem biztonságos teljesen automatikusan eldönteni.</Notice>}
              <label className="field">
                <span>Időbeli szűrés</span>
                <select value={temporalFilter} onChange={(event) => { setTemporalFilter(event.target.value); setValidation(null); }}>
                  <option value="progressive">Progresszív — nincs időbeli szűrés</option>
                  <option value="ivtc_tff">IVTC — felső mező először</option>
                  <option value="ivtc_bff">IVTC — alsó mező először</option>
                  <option value="bwdif_tff">BWDIF — felső mező először</option>
                  <option value="bwdif_bff">BWDIF — alsó mező először</option>
                  <option value="hybrid_safe_bob_tff">Hibrid safe bob — TFF</option>
                  <option value="hybrid_safe_bob_bff">Hibrid safe bob — BFF</option>
                </select>
              </label>
              <CropEditor crop={crop} width={videoStream?.video?.width ?? 1920} height={videoStream?.video?.height ?? 1080} onChange={(next) => { setCrop(next); setValidation(null); }} />
            </Card>
          </div>

          <aside className="video-settings-side">
            <Card className="source-facts-card">
              <span className="eyebrow">Scanből rögzítve</span>
              <h3>Forrásparaméterek</h3>
              <dl className="summary-list">
                <div><dt>Kimeneti kodek</dt><dd>{encoder}</dd></div>
                <div><dt>Forrás</dt><dd>{videoStream?.video?.codec?.toUpperCase() || "—"}</dd></div>
                <div><dt>Bitmélység</dt><dd>{videoStream?.video?.bit_depth ?? "—"} bit</dd></div>
                <div><dt>Képsebesség</dt><dd>{videoStream?.video?.frame_rate || "—"}</dd></div>
                <div><dt>Színtér</dt><dd>{videoStream?.video?.color_primaries || "—"}</dd></div>
                <div><dt>HDR10</dt><dd>{videoStream?.video?.hdr10 ? "Megtartva" : "Nincs"}</dd></div>
              </dl>
              {videoStream?.video?.dolby_vision && <Notice tone="warning">Dolby Vision nem kerül megtartásra; a HDR10 alpréteg lesz a kimenet.</Notice>}
            </Card>
          </aside>
        </div>
      )}

      {step === 4 && (
        <div className="review-layout">
          <Card className="review-main-card">
            <span className="eyebrow">Végleges ellenőrzés</span>
            <h3>{job.name}</h3>
            <div className="review-summary-grid">
              <div><ListVideo size={18} /><span><small>Playlist</small><strong>{playlistId} · {formatDuration(playlist?.duration_seconds)}</strong></span></div>
              <div><Music size={18} /><span><small>Megtartott sávok</small><strong>{retainedTracks.length}</strong></span></div>
              <div><Settings2 size={18} /><span><small>Videóprofil</small><strong>{encoder} · {detailLevel}</strong></span></div>
              <div><Sparkles size={18} /><span><small>Comparison</small><strong>I / P / B · PNG</strong></span></div>
            </div>

            <label className="field">
              <span>Kimeneti fájlnév</span>
              <div className="input-suffix"><input value={outputName} onChange={(event) => { setOutputName(event.target.value); setValidation(null); }} /><span>.mkv</span></div>
            </label>

            <div className="review-options">
              <label className="toggle-row">
                <span><strong>Képfeltöltés és BBCode</strong><small>Automatikus módban a sorrend: ImgBB, Catbox, majd Freeimage; a sikeres szolgáltató az egész csomagra rögzül.</small></span>
                <input type="checkbox" checked={uploadImages} onChange={(event) => { setUploadImages(event.target.checked); setValidation(null); }} /><span className="toggle" aria-hidden="true" />
              </label>
              <label className="field">
                <span>Képtárhely</span>
                <select aria-label="Képtárhely" value={selectedImageProvider} disabled={!uploadImages} onChange={(event) => { setSelectedImageProvider(event.target.value as ImageUploadProvider); setValidation(null); }}>
                  {Object.entries(IMAGE_UPLOAD_PROVIDER_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
                <small>Automatikus módban csak az első sikeres kép előtt válthat szolgáltatót; kézi módban nincs failover.</small>
              </label>
              <label className="toggle-row">
                <span><strong>Szigorú I/P/B típusazonosság · kötelező</strong><small>Progresszív forrásnál a source és az encode képtípusa mindig azonos; ez nem kapcsolható ki.</small></span>
                <input type="checkbox" checked disabled aria-label="Szigorú I/P/B típusazonosság kötelező" /><span className="toggle" aria-hidden="true" />
              </label>
            </div>

            {!validation && (
              <Notice tone="info" title="Még nincs jóváhagyva">Az „Ellenőrzés” gomb a backend valódi plannerével validálja a sávokat, cropot, HDR-t és x264/x265 paramétereket, de még nem indít kódolást.</Notice>
            )}
            {needsColorConfirmation && !colorConfirmed && (
              <Notice tone="warning" title="A forrás színadatait még jóvá kell hagynod">
                <p>A lemezből hiányzik: {reportedMissingColorFields.map((field) => SOURCE_COLOR_FIELD_LABELS[field]).join(", ")}.</p>
                <p>{safeColorRecommendation
                  ? "A forrás jellemzői alapján ajánlott biztonságos értékeket egy érintéssel jóváhagyhatod; ez nem végez színkonverziót."
                  : "Ehhez a forráshoz nem adható biztonságos automatikus alapérték. Nyisd meg a mezőket, és csak ellenőrzött értékeket adj meg."}</p>
                {safeColorRecommendation
                  ? <Button variant="secondary" icon={<Palette size={17} />} onClick={confirmSourceColor}>Ajánlott értékek jóváhagyása</Button>
                  : <Button variant="secondary" icon={<Palette size={17} />} onClick={() => setStep(3)}>Színadatok kézi megadása</Button>}
              </Notice>
            )}
            {validate.isError && !colorApiIssue && <Notice tone="danger" title="A terv nem indítható">{validate.error instanceof ApiError ? validate.error.detail : validate.error.message}</Notice>}
            {validate.isError && colorApiIssue && (
              <Notice tone="danger" title="Hiányos forrás-színinformáció">
                <p>A kódolás biztonsága érdekében erősítsd meg ezeket: {reportedMissingColorFields.map((field) => SOURCE_COLOR_FIELD_LABELS[field]).join(", ")}.</p>
                <Button variant="secondary" onClick={() => setStep(3)}>Színadatok megnyitása</Button>
              </Notice>
            )}
            {save.isError && <Notice tone="danger" title="A jóváhagyás nem menthető">{save.error instanceof ApiError ? save.error.detail : save.error.message}</Notice>}
          </Card>

          <Card className={validation ? "validation-card validation-card--success" : "validation-card"}>
            <span className="validation-card__icon">{validation ? <Check size={26} /> : <SlidersHorizontal size={26} />}</span>
            <span className="eyebrow">Szerveroldali planner</span>
            <h3>{validation ? "A terv érvényes" : "Ellenőrzésre vár"}</h3>
            <p>{validation ? "A tényleges effektív profil elkészült. Jóváhagyás után a worker automatikusan folytatja." : "A backend ugyanazzal a logikával ellenőriz, amelyet a worker kódoláskor használ."}</p>
            {validation && (
              <>
                <dl className="summary-list">
                  <div><dt>Kódoló</dt><dd>{validation.encoder}</dd></div>
                  <div><dt>CRF</dt><dd>{String(validation.settings.crf)}</dd></div>
                  <div><dt>Preset</dt><dd>{String(validation.settings.preset)}</dd></div>
                  <div><dt>Profil</dt><dd>{String(validation.settings.profile)}</dd></div>
                  <div><dt>Crop</dt><dd>{Object.values(validation.crop).join(" / ")}</dd></div>
                </dl>
                {validation.advisory_warnings.length > 0 && <Notice tone="warning"><ul>{validation.advisory_warnings.map((item) => <li key={item}>{item}</li>)}</ul></Notice>}
                <details className="command-preview"><summary><Copy size={15} /> FFmpeg videóparaméterek</summary><code>{validation.ffmpeg_video_args.join(" ")}</code></details>
              </>
            )}
            {!validation ? (
              <Button icon={<Check size={17} />} onClick={() => validate.mutate()} loading={validate.isPending} disabled={needsColorConfirmation && !colorConfirmed}>Terv ellenőrzése</Button>
            ) : (
              <Button icon={<Clapperboard size={18} />} onClick={() => save.mutate()} loading={save.isPending}>Jóváhagyás és automatikus indítás</Button>
            )}
          </Card>
        </div>
      )}

      <div className="wizard-footer">
        <Button variant="ghost" icon={<ArrowLeft size={17} />} onClick={() => setStep((value) => Math.max(1, value - 1))} disabled={step === 1}>Vissza</Button>
        {step < 4 && <Button icon={<ArrowRight size={17} />} onClick={() => { setStep((value) => Math.min(4, value + 1)); setValidation(null); }} disabled={!canNext}>Tovább</Button>}
      </div>
    </div>
  );
}

const SOURCE_COLOR_OPTIONS: Record<SourceColorField, Array<{ value: string; label: string }>> = {
  primaries: [
    { value: "bt709", label: "BT.709" },
    { value: "bt2020", label: "BT.2020" },
    { value: "smpte170m", label: "SMPTE 170M" },
    { value: "smpte240m", label: "SMPTE 240M" },
    { value: "bt470m", label: "BT.470 M" },
    { value: "bt470bg", label: "BT.470 BG" },
  ],
  transfer: [
    { value: "bt709", label: "BT.709" },
    { value: "smpte2084", label: "PQ / SMPTE ST 2084" },
    { value: "arib-std-b67", label: "HLG / ARIB STD-B67" },
    { value: "smpte170m", label: "SMPTE 170M" },
    { value: "smpte240m", label: "SMPTE 240M" },
    { value: "bt470m", label: "BT.470 M" },
    { value: "bt470bg", label: "BT.470 BG" },
    { value: "linear", label: "Lineáris" },
  ],
  matrix: [
    { value: "bt709", label: "BT.709" },
    { value: "bt2020nc", label: "BT.2020 nem konstans fényesség" },
    { value: "bt2020c", label: "BT.2020 konstans fényesség" },
    { value: "smpte170m", label: "SMPTE 170M" },
    { value: "bt470bg", label: "BT.470 BG" },
    { value: "rgb", label: "RGB" },
  ],
  range: [
    { value: "limited", label: "Korlátozott / TV" },
    { value: "full", label: "Teljes / PC" },
  ],
  chroma_location: [
    { value: "left", label: "Bal" },
    { value: "center", label: "Közép" },
    { value: "topleft", label: "Bal felső" },
    { value: "top", label: "Felső" },
    { value: "bottomleft", label: "Bal alsó" },
    { value: "bottom", label: "Alsó" },
  ],
};

function SourceColorConfirmation({
  video,
  discKind,
  missing,
  value,
  confirmed,
  safeRecommendation,
  onChange,
  onConfirm,
}: {
  video: MediaStream["video"] | undefined;
  discKind: DiscScanResult["disc_kind"];
  missing: SourceColorField[];
  value: SourceColorMetadata;
  confirmed: boolean;
  safeRecommendation: boolean;
  onChange: (value: SourceColorMetadata) => void;
  onConfirm: () => void;
}) {
  const profileName = discKind === "uhd" && video?.hdr10
    ? "HDR10 UHD Blu-ray · BT.2020 / PQ"
    : "SDR Blu-ray · BT.709";
  const fields = Object.keys(SOURCE_COLOR_FIELD_LABELS) as SourceColorField[];
  const complete = Object.values(value).every((item) => Boolean(item));
  const manualReason = discKind === "uhd" && !video?.hdr10
    ? "Az SDR UHD-forrás színtere nem következtethető ki biztonságosan a lemeztípusból."
    : "Automatikus BT.709 csak legalább 1280×720-as, 8 bites SDR Blu-ray forráshoz használható biztonságosan.";

  return (
    <Card className={confirmed ? "source-color-card source-color-card--confirmed" : "source-color-card"}>
      <div className="section-heading">
        <div>
          <span className="section-heading__icon"><Palette size={19} /></span>
          <div>
            <h3>Forrás színinformációjának megerősítése</h3>
            <p>A scan nem tudott minden kötelező jelölést kiolvasni</p>
          </div>
        </div>
        <Badge tone={confirmed ? "success" : "warning"}>{confirmed ? "Jóváhagyva" : "Teendő"}</Badge>
      </div>

      <div className="source-color-missing" aria-label="Hiányzó forrásadatok">
        <strong>Hiányzik a lemezből:</strong>
        <div>{missing.map((field) => <Badge key={field} tone="warning">{SOURCE_COLOR_FIELD_LABELS[field]}</Badge>)}</div>
      </div>

      <Notice tone={confirmed ? "success" : safeRecommendation ? "info" : "warning"} title={confirmed ? "A színjelölés megerősítve" : safeRecommendation ? `Ajánlott alapérték: ${profileName}` : "Kézi ellenőrzés szükséges"}>
        {confirmed
          ? "A kódoló a jóváhagyott jelölést írja a kimenetbe. Színkonverzió nem történik."
          : safeRecommendation
            ? "A lemeztípus, a felbontás, a bitmélység és a HDR-jelzés alapján töltöttük ki. Nézd át, majd hagyd jóvá; ettől még nem indul el a kódolás."
            : `${manualReason} Válaszd ki a lemez dokumentációjával vagy hiteles elemzéssel ellenőrzött értékeket.`}
      </Notice>

      <details className="source-color-details" open={!confirmed}>
        <summary>{confirmed ? "Jóváhagyott értékek megtekintése" : "Ajánlott értékek ellenőrzése"}</summary>
        <div className="source-color-fields">
          {fields.map((field) => {
            const editable = missing.includes(field);
            const options = SOURCE_COLOR_OPTIONS[field];
            const knownOption = options.some((option) => option.value === value[field]);
            return (
              <label key={field} className="source-color-field">
                <span>
                  <strong>{SOURCE_COLOR_FIELD_LABELS[field]}</strong>
                  <small>{editable ? "Hiányzott · ajánlott érték" : "A scanből rögzítve / BD-alapérték"}</small>
                </span>
                <select
                  value={value[field]}
                  disabled={!editable}
                  onChange={(event) => onChange({ ...value, [field]: event.target.value })}
                >
                  {!value[field] && <option value="">— Válassz ellenőrzött értéket —</option>}
                  {!knownOption && value[field] && <option value={value[field]}>{value[field]}</option>}
                  {options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                </select>
              </label>
            );
          })}
        </div>
      </details>

      {!confirmed && (
        <Button className="source-color-confirm" icon={<Check size={17} />} onClick={onConfirm} disabled={!complete}>
          {safeRecommendation ? "Ezeknek az értékeknek a jóváhagyása" : "A kézzel ellenőrzött értékek jóváhagyása"}
        </Button>
      )}
    </Card>
  );
}

function TrackTable({
  title,
  icon,
  streams,
  selections,
  onUpdate,
}: {
  title: string;
  icon: ReactNode;
  streams: MediaStream[];
  selections: TrackSelection[];
  onUpdate: (streamId: string, update: Partial<TrackSelection>) => void;
}) {
  return (
    <Card className="track-card">
      <div className="section-heading"><div><span className="section-heading__icon">{icon}</span><div><h3>{title}</h3><p>{streams.length} sáv a kiválasztott playlistben</p></div></div></div>
      {!streams.length ? <p className="muted">Nincs ilyen sáv.</p> : (
        <div className="track-table">
          {streams.map((stream) => {
            const selection = selections.find((item) => item.stream_id === stream.id);
            if (!selection) {
              return <Notice key={stream.id} tone="danger">A(z) {stream.id} sávhoz nem készült választási terv. Válaszd ki újra a playlistet.</Notice>;
            }
            const uncertain = !selection.language || stream.language?.needs_review;
            const actionDetails = AUDIO_ACTION_DETAILS[selection.action];
            const sourceDetails = [
              stream.codec.toUpperCase(),
              stream.codec_profile,
              stream.channels ? `${stream.channels} csatorna` : null,
              stream.channel_layout,
              stream.sample_rate ? `${stream.sample_rate / 1000} kHz` : null,
              stream.bit_depth ? `${stream.bit_depth} bit` : null,
              stream.object_audio ? "objektumalapú hang" : null,
            ].filter(Boolean).join(" · ");
            return (
              <div key={stream.id} className={selection.action === "omit" ? "track-row track-row--omitted" : "track-row"}>
                <div className="track-row__identity">
                  <span className="track-row__type">{stream.kind === "audio" ? <Music size={17} /> : <Subtitles size={17} />}</span>
                  <span><strong>{stream.title || `${stream.codec.toUpperCase()} sáv`}</strong><small>{sourceDetails}</small></span>
                </div>
                <label className="track-language">
                  <Languages size={16} aria-hidden="true" />
                  <input
                    value={selection.language || ""}
                    onChange={(event) => onUpdate(stream.id, { language: event.target.value.trim() || null })}
                    placeholder="pl. hun"
                    maxLength={35}
                    aria-label={`${stream.title || stream.id} nyelve`}
                  />
                  {uncertain && <span role="img" aria-label="Bizonytalan vagy hiányzó nyelv" title="Bizonytalan vagy hiányzó nyelv"><AlertTriangle size={15} aria-hidden="true" /></span>}
                </label>
                <div className={stream.kind === "audio" ? "track-actions track-actions--audio" : "track-actions"} role="group" aria-label={`${stream.title || stream.id} kezelése`}>
                  {(stream.kind === "audio" ? AUDIO_TRACK_ACTIONS : ["copy", "omit"] as TrackAction[]).map((action) => (
                    <button type="button" key={action} className={selection.action === action ? "active" : ""} aria-pressed={selection.action === action} onClick={() => onUpdate(stream.id, { action: action as TrackAction })}>
                      {AUDIO_ACTION_DETAILS[action].label}
                    </button>
                  ))}
                </div>
                {stream.kind === "audio" && <div className="track-target-note"><strong>{actionDetails.label}:</strong> {actionDetails.description}</div>}
                {selection.action !== "omit" && (
                  <div className="track-flags">
                    <label><input type="checkbox" checked={selection.default} onChange={(event) => onUpdate(stream.id, { default: event.target.checked })} /> Alapértelmezett</label>
                    <label><input type="checkbox" checked={selection.forced} onChange={(event) => onUpdate(stream.id, { forced: event.target.checked })} /> Kényszerített</label>
                  </div>
                )}
                {stream.object_audio && AUDIO_TRANSCODE_ACTIONS.has(selection.action) && <div className="track-warning">Átalakításkor az Atmos/DTS:X objektum-metaadat elvész; a csatornaalapú hangsáv marad meg.</div>}
                {stream.kind === "audio" && stream.channels && stream.channels > 6 && ["ac3", "eac3", "dts"].includes(selection.action) && <div className="track-warning">A {stream.channels} csatornás forrás ennél a célnál ellenőrzötten 5.1-re lesz keverve.</div>}
                {stream.kind === "audio" && selection.action === "dts" && /dts/i.test(`${stream.codec} ${stream.codec_profile || ""}`) && /hd/i.test(`${stream.codec} ${stream.codec_profile || ""}`) && <div className="track-target-note">A beágyazott DTS core újrakódolás nélkül lesz kinyerve.</div>}
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

function ProfileFields({
  fields,
  settings,
  recommendation,
  search,
  onSearch,
  onUpdate,
  onSettings,
}: {
  fields: FieldSpec[];
  settings: Record<string, unknown>;
  recommendation: Record<string, unknown>;
  search: string;
  onSearch: (value: string) => void;
  onUpdate: (field: FieldSpec, raw: string | boolean) => void;
  onSettings: Dispatch<SetStateAction<Record<string, unknown>>>;
}) {
  const filtered = fields.filter((field) => {
    if (LOCKED_FIELDS.has(field.name)) return false;
    const needle = search.trim().toLocaleLowerCase("hu");
    return !needle || `${field.name} ${fieldLabel(field.name)} ${field.description}`.toLocaleLowerCase("hu").includes(needle);
  });
  const grouped = filtered.reduce<Record<string, FieldSpec[]>>((result, field) => {
    (result[field.group] ??= []).push(field);
    return result;
  }, {});
  const groups = Object.entries(grouped);
  return (
    <>
      {fields.length > 14 && (
        <label className="search-field settings-search"><Search size={16} aria-hidden="true" /><input aria-label="Kódolóparaméter keresése" value={search} onChange={(event) => onSearch(event.target.value)} placeholder="Paraméter keresése…" /></label>
      )}
      <div className="profile-groups">
        {groups.map(([group, groupFields]) => (
          <ProfileGroup key={group} initiallyOpen={group === "rate_control" || group === "gop" || fields.length < 15}>
            <summary><span>{GROUP_LABELS[group] || humanize(group)}</span><Badge>{groupFields.length}</Badge><ChevronDown size={17} /></summary>
            <div className="profile-fields">
              {groupFields.map((field) => field.name === "vbv" ? (
                <VbvField key={field.name} value={settings.vbv} onChange={(value) => onSettings((current) => ({ ...current, vbv: value }))} />
              ) : (
                <ProfileField key={field.name} field={field} value={settings[field.name] ?? recommendation[field.name] ?? field.default} onUpdate={onUpdate} />
              ))}
            </div>
          </ProfileGroup>
        ))}
      </div>
    </>
  );
}

function ProfileGroup({ initiallyOpen, children }: { initiallyOpen: boolean; children: ReactNode }) {
  const [open, setOpen] = useState(initiallyOpen);
  return (
    <details className="profile-group" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      {children}
    </details>
  );
}

function ProfileField({ field, value, onUpdate }: { field: FieldSpec; value: unknown; onUpdate: (field: FieldSpec, raw: string | boolean) => void }) {
  const description = FIELD_HELP[field.name] || field.description;
  if (field.value_type === "boolean") {
    return (
      <label className="parameter-field parameter-field--toggle">
        <span><strong>{fieldLabel(field.name)}</strong>{description && <small>{description}</small>}</span>
        <input type="checkbox" checked={Boolean(value)} onChange={(event) => onUpdate(field, event.target.checked)} /><span className="toggle" />
      </label>
    );
  }
  return (
    <label className="parameter-field">
      <span><strong>{fieldLabel(field.name)}</strong>{description && <small>{description}</small>}</span>
      {field.value_type === "enum" ? (
        <select value={String(value ?? "")} onChange={(event) => onUpdate(field, event.target.value)}>
          {field.choices.map((choice) => <option key={choice} value={choice}>{choice}</option>)}
        </select>
      ) : (
        <input
          type={field.value_type === "number" || field.value_type === "integer" ? "number" : "text"}
          value={String(value ?? "")}
          min={field.minimum ?? undefined}
          max={field.maximum ?? undefined}
          step={field.value_type === "integer" ? 1 : field.value_type === "number" ? 0.05 : undefined}
          onChange={(event) => onUpdate(field, event.target.value)}
        />
      )}
    </label>
  );
}

function VbvField({ value, onChange }: { value: unknown; onChange: (value: unknown) => void }) {
  const enabled = Boolean(value && typeof value === "object");
  const current = enabled ? value as Record<string, unknown> : {};
  return (
    <div className="parameter-field parameter-field--object">
      <label className="toggle-row toggle-row--compact">
        <span><strong>VBV korlátozás</strong><small>Csak konkrét lejátszói/level kompatibilitási igénynél szükséges.</small></span>
        <input type="checkbox" checked={enabled} onChange={(event) => onChange(event.target.checked ? { maxrate_kbps: 40000, bufsize_kbps: 50000, initial_fullness: 0.9 } : null)} /><span className="toggle" />
      </label>
      {enabled && <div className="object-fields">
        <label>Maxrate (kb/s)<input type="number" value={String(current.maxrate_kbps ?? 40000)} onChange={(event) => onChange({ ...current, maxrate_kbps: Number(event.target.value) })} /></label>
        <label>Buffer (kb)<input type="number" value={String(current.bufsize_kbps ?? 50000)} onChange={(event) => onChange({ ...current, bufsize_kbps: Number(event.target.value) })} /></label>
        <label>Kezdeti telítettség<input type="number" min="0" max="1" step="0.05" value={String(current.initial_fullness ?? 0.9)} onChange={(event) => onChange({ ...current, initial_fullness: Number(event.target.value) })} /></label>
      </div>}
    </div>
  );
}

function CropEditor({
  crop,
  width,
  height,
  onChange,
}: {
  crop: { left: number; top: number; right: number; bottom: number };
  width: number;
  height: number;
  onChange: (value: { left: number; top: number; right: number; bottom: number }) => void;
}) {
  const sourceWidth = Math.max(2, width || 1920);
  const sourceHeight = Math.max(2, height || 1080);
  const maxHorizontal = Math.max(0, Math.floor(sourceWidth / 3 / 2) * 2);
  const maxVertical = Math.max(0, Math.floor(sourceHeight / 3 / 2) * 2);
  const innerWidth = Math.max(2, 100 - ((crop.left + crop.right) / sourceWidth) * 100);
  const innerHeight = Math.max(2, 100 - ((crop.top + crop.bottom) / sourceHeight) * 100);
  const normalizeCrop = (raw: string, maximum: number) =>
    Math.min(maximum, Math.max(0, Math.round((Number(raw) || 0) / 2) * 2));
  return (
    <div className="crop-editor">
      <div className="crop-preview" style={{ aspectRatio: `${sourceWidth} / ${sourceHeight}` }}>
        <div className="crop-preview__frame" style={{
          left: `${(crop.left / sourceWidth) * 100}%`,
          right: `${(crop.right / sourceWidth) * 100}%`,
          top: `${(crop.top / sourceHeight) * 100}%`,
          bottom: `${(crop.bottom / sourceHeight) * 100}%`,
        }}>
          <span>{Math.round(sourceWidth - crop.left - crop.right)} × {Math.round(sourceHeight - crop.top - crop.bottom)}</span>
        </div>
        <div className="crop-preview__grid" />
        <small>{innerWidth.toFixed(0)}% × {innerHeight.toFixed(0)}% megmarad</small>
      </div>
      <div className="crop-controls">
        {(["top", "bottom", "left", "right"] as const).map((side) => {
          const max = side === "top" || side === "bottom" ? maxVertical : maxHorizontal;
          const label = side === "top" ? "Fent" : side === "bottom" ? "Lent" : side === "left" ? "Bal" : "Jobb";
          return (
            <div className="crop-control" key={side}>
              <span>{label}<input aria-label={`${label} crop pixelben`} type="number" min="0" max={max} step="2" value={crop[side]} onChange={(event) => onChange({ ...crop, [side]: normalizeCrop(event.target.value, max) })} /></span>
              <input aria-label={`${label} crop csúszka`} type="range" min="0" max={max} step="2" value={crop[side]} onChange={(event) => onChange({ ...crop, [side]: normalizeCrop(event.target.value, max) })} />
            </div>
          );
        })}
      </div>
      <small className="field-help">A crop értékek páros pixelekre állnak. A backend a forrásméretet és a kimeneti kompatibilitást ismét ellenőrzi.</small>
    </div>
  );
}

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookMarked, Download, Save, Trash2, Upload } from "lucide-react";
import { useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import type {
  AutoCrfConfig,
  DetailLevel,
  DynamicHdrMode,
  LibraryImportResult,
  LibraryProfile,
  LibraryProfileSelection,
} from "../api/types";
import { downloadJson } from "../utils";
import { Badge, Button, Card, LoadingPanel, Modal, Notice } from "./ui";

/** Disc-specific or bitstream-policy fields: never part of a shared profile. */
export const NON_PORTABLE_SETTINGS = [
  "encoder",
  "detail_level",
  "profile",
  "level",
  "bit_depth",
  "pixel_format",
  "color",
  "vbv",
  "hdr10",
  "aud",
  "repeat_headers",
  "annexb",
] as const;

export function portableSettings(settings: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(settings).filter(([key]) => !(NON_PORTABLE_SETTINGS as readonly string[]).includes(key)),
  );
}

function readText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(new Error("A fájl nem olvasható."));
    reader.readAsText(file);
  });
}

function message(error: unknown): string {
  return error instanceof ApiError ? error.detail : error instanceof Error ? error.message : "Ismeretlen hiba";
}

export function ProfileLibraryPanel({
  encoder,
  detailLevel,
  settings,
  autoCrf,
  dynamicHdr,
  onApply,
}: {
  encoder: "x264" | "x265";
  detailLevel: DetailLevel;
  settings: Record<string, unknown>;
  autoCrf: AutoCrfConfig | null;
  dynamicHdr: DynamicHdrMode;
  onApply: (selection: LibraryProfileSelection, profile: LibraryProfile) => void;
}) {
  const queryClient = useQueryClient();
  const library = useQuery({ queryKey: ["profile-library"], queryFn: api.profileLibrary, retry: false });
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [applied, setApplied] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<LibraryProfile | null>(null);
  const [importResult, setImportResult] = useState<LibraryImportResult | null>(null);
  const [importMode, setImportMode] = useState<"rename" | "skip" | "overwrite">("rename");
  const [localError, setLocalError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["profile-library"] });
  const buildDocument = () => ({
    name: name.trim(),
    description: description.trim(),
    encoder,
    detail_level: detailLevel,
    settings: portableSettings(settings),
    ...(autoCrf?.enabled ? { auto_crf: autoCrf } : {}),
    ...(dynamicHdr !== "discard" ? { dynamic_hdr: dynamicHdr } : {}),
  });

  const save = useMutation({
    mutationFn: (overwrite: boolean) => api.saveLibraryProfile(buildDocument(), overwrite),
    onSuccess: () => {
      setConflict(false);
      setName("");
      setDescription("");
      refresh();
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) setConflict(true);
    },
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteLibraryProfile(id),
    onSuccess: () => {
      setDeleteTarget(null);
      refresh();
    },
  });
  const importer = useMutation({
    mutationFn: (payload: Record<string, unknown>) => api.importLibraryProfiles(payload, importMode),
    onSuccess: (result) => {
      setImportResult(result);
      refresh();
    },
  });

  async function exportOne(profile: LibraryProfile) {
    try {
      downloadJson(`${profile.id}.bdencode-profile.json`, await api.exportLibraryProfile(profile.id));
    } catch (error) {
      setLocalError(message(error));
    }
  }
  async function exportAll() {
    try {
      downloadJson("bdencode-profiles.json", await api.exportLibrary());
    } catch (error) {
      setLocalError(message(error));
    }
  }
  async function readFile(file: File | undefined) {
    if (!file) return;
    setLocalError(null);
    setImportResult(null);
    try {
      const parsed: unknown = JSON.parse(await readText(file));
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("A fájl nem BDEncode profil vagy profilcsomag.");
      }
      importer.mutate(parsed as Record<string, unknown>);
    } catch (error) {
      setLocalError(error instanceof SyntaxError ? "A fájl nem érvényes JSON." : message(error));
    } finally {
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  const items = library.data?.items ?? [];
  const matching = items.filter((item) => item.encoder === encoder);
  const hidden = items.length - matching.length;

  return (
    <Card className="settings-card profile-library">
      <div className="section-heading">
        <div>
          <span className="section-heading__icon"><BookMarked size={19} /></span>
          <div><h3>Profilkönyvtár</h3><p>Mentett és megosztható kódolási profilok · csak hordozható beállítások</p></div>
        </div>
        <div className="profile-library__tools">
          <Button variant="ghost" icon={<Download size={16} />} onClick={() => void exportAll()} disabled={items.length === 0}>Teljes export</Button>
          <Button variant="ghost" icon={<Upload size={16} />} loading={importer.isPending} onClick={() => fileInput.current?.click()}>Import</Button>
          <select value={importMode} onChange={(event) => setImportMode(event.target.value as typeof importMode)} aria-label="Név ütközése importnál">
            <option value="rename">Ütközés: átnevezés</option>
            <option value="skip">Ütközés: kihagyás</option>
            <option value="overwrite">Ütközés: felülírás</option>
          </select>
          <input
            ref={fileInput}
            type="file"
            accept="application/json,.json"
            hidden
            data-testid="profile-import-input"
            onChange={(event) => void readFile(event.target.files?.[0])}
          />
        </div>
      </div>

      {library.isLoading ? <LoadingPanel label="Profilok betöltése…" /> : library.isError ? (
        <Notice tone="warning">A profilkönyvtár nem érhető el: {message(library.error)}</Notice>
      ) : matching.length === 0 ? (
        <p className="muted">Még nincs mentett {encoder} profil. Állítsd be a kódolást, majd mentsd el lentebb.</p>
      ) : (
        <ul className="profile-library__list">
          {matching.map((profile) => (
            <li key={profile.id} className="profile-library__item">
              <div>
                <strong>{profile.name}</strong>
                <span className="profile-library__badges">
                  <Badge>{profile.detail_level}</Badge>
                  {profile.auto_crf?.enabled && <Badge tone="info">VMAF {profile.auto_crf.target_vmaf}</Badge>}
                  {profile.dynamic_hdr && profile.dynamic_hdr !== "discard" && <Badge tone="warning">{profile.dynamic_hdr}</Badge>}
                </span>
                {profile.description && <p>{profile.description}</p>}
              </div>
              <div className="profile-library__actions">
                <Button
                  variant="secondary"
                  onClick={() => {
                    onApply(profile.selection, profile);
                    setApplied(profile.name);
                  }}
                  aria-label={`${profile.name} alkalmazása`}
                >
                  Alkalmaz
                </Button>
                <Button variant="ghost" icon={<Download size={15} />} onClick={() => void exportOne(profile)} aria-label={`${profile.name} exportálása`}>Export</Button>
                <button type="button" className="icon-button" aria-label={`${profile.name} törlése`} onClick={() => { remove.reset(); setDeleteTarget(profile); }}>
                  <Trash2 size={15} aria-hidden="true" />
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {hidden > 0 && <p className="muted">{hidden} másik kodekhez tartozó profil rejtve.</p>}
      {applied && <Notice tone="success">A(z) „{applied}” profil bekerült a szerkeszthető mezőkbe. A végleges ellenőrzés továbbra is a Terv ellenőrzése lépésben történik.</Notice>}
      {localError && <Notice tone="danger">{localError}</Notice>}
      {importer.isError && <Notice tone="danger" title="Az import sikertelen">{message(importer.error)}</Notice>}
      {importResult && (
        <Notice tone={importResult.errors.length ? "warning" : "success"} title="Import eredménye">
          {importResult.imported.length} importálva, {importResult.skipped.length} kihagyva, {importResult.errors.length} hibás.
          {importResult.errors.length > 0 && (
            <ul>{importResult.errors.map((item, index) => <li key={index}>{item.name}: {item.message}</li>)}</ul>
          )}
        </Notice>
      )}

      <form
        className="profile-library__save"
        onSubmit={(event) => {
          event.preventDefault();
          if (name.trim()) save.mutate(false);
        }}
      >
        <label className="field">
          <span>Aktuális beállítások mentése profilként</span>
          <input value={name} onChange={(event) => { setName(event.target.value); setConflict(false); }} placeholder="Profil neve" maxLength={80} aria-label="Profil neve" />
        </label>
        <label className="field">
          <span>Leírás (nem kötelező)</span>
          <input value={description} onChange={(event) => setDescription(event.target.value)} placeholder="Mire való?" maxLength={500} aria-label="Profil leírása" />
        </label>
        <Button type="submit" icon={<Save size={16} />} loading={save.isPending} disabled={!name.trim()}>Mentés</Button>
      </form>
      {save.isError && !conflict && <Notice tone="danger" title="A profil nem menthető">{message(save.error)}</Notice>}
      {save.isSuccess && <Notice tone="success">A profil elmentve.</Notice>}
      {conflict && (
        <Notice tone="warning" title="Már van ilyen nevű profil">
          <Button variant="secondary" onClick={() => save.mutate(true)} loading={save.isPending}>Felülírás</Button>
        </Notice>
      )}

      <Modal
        open={deleteTarget !== null}
        title="Törlöd a profilt?"
        busy={remove.isPending}
        onClose={() => { if (!remove.isPending) setDeleteTarget(null); }}
        footer={
          <>
            <Button variant="ghost" disabled={remove.isPending} onClick={() => setDeleteTarget(null)}>Mégse</Button>
            <Button variant="danger" loading={remove.isPending} onClick={() => deleteTarget && remove.mutate(deleteTarget.id)}>Törlés</Button>
          </>
        }
      >
        <p>A(z) „{deleteTarget?.name}” profil végleg törlődik a könyvtárból. A már elmentett munkák beállításait ez nem érinti.</p>
        {remove.isError && <Notice tone="danger">{message(remove.error)}</Notice>}
      </Modal>
    </Card>
  );
}

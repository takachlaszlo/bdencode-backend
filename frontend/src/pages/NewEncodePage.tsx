import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  Clapperboard,
  Disc3,
  Film,
  Folder,
  FolderOpen,
  GraduationCap,
  HardDrive,
  Layers3,
  Music2,
  RefreshCw,
  Search,
  Sparkles,
  Tv2,
  UploadCloud,
  Wrench,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router";
import { api, ApiError } from "../api/client";
import type { ContentType, DetailLevel, DiscType, SourceEntry } from "../api/types";
import { Badge, Button, Card, LoadingPanel, Notice, PageHeader } from "../components/ui";
import { basename, CONTENT_LABELS } from "../utils";

interface Draft {
  sourcePath: string;
  sourceName: string;
  name: string;
  discType: DiscType;
  contentType: ContentType;
  detailLevel: DetailLevel;
  uploadImages: boolean;
}

const defaultDraft: Draft = {
  sourcePath: "",
  sourceName: "",
  name: "",
  discType: "AUTO",
  contentType: "FILM",
  detailLevel: "beginner",
  uploadImages: true,
};

const contentOptions = [
  { value: "FILM" as const, icon: Film, title: "Film", description: "Egy vagy több filmváltozat, fejezetekkel." },
  { value: "CONCERT" as const, icon: Music2, title: "Koncert", description: "Zene- és dinamikaérzékeny hangkezelés." },
  { value: "ANIME" as const, icon: Sparkles, title: "Anime", description: "Animációhoz hangolt pszichovizuális profil." },
  { value: "SERIES" as const, icon: Tv2, title: "Sorozatlemez", description: "Epizódok és playlist-csoportok kezelése." },
];

const detailOptions = [
  { value: "beginner" as const, icon: GraduationCap, title: "Kezdő", description: "A rendszer ajánl, neked csak a fontos döntéseket kell meghoznod." },
  { value: "advanced" as const, icon: Wrench, title: "Haladó", description: "CRF, preset, GOP, AQ és a fontosabb képi paraméterek." },
  { value: "pro" as const, icon: Layers3, title: "Profi", description: "Minden támogatott x264/x265 paraméter, csoportosítva." },
];

function loadDraft(): Draft {
  try {
    const stored = localStorage.getItem("bdencode:new-job-draft");
    if (!stored) return defaultDraft;
    const parsed = JSON.parse(stored) as unknown;
    if (!parsed || typeof parsed !== "object") return defaultDraft;
    const value = parsed as Record<string, unknown>;
    const discType = value.discType === "BD" || value.discType === "UHD" || value.discType === "AUTO"
      ? value.discType
      : defaultDraft.discType;
    const contentType = value.contentType === "CONCERT" || value.contentType === "ANIME" || value.contentType === "SERIES" || value.contentType === "FILM"
      ? value.contentType
      : defaultDraft.contentType;
    const detailLevel = value.detailLevel === "advanced" || value.detailLevel === "pro" || value.detailLevel === "beginner"
      ? value.detailLevel
      : defaultDraft.detailLevel;
    return {
      sourcePath: typeof value.sourcePath === "string" ? value.sourcePath : "",
      sourceName: typeof value.sourceName === "string" ? value.sourceName : "",
      name: typeof value.name === "string" ? value.name : "",
      discType,
      contentType,
      detailLevel,
      uploadImages: typeof value.uploadImages === "boolean" ? value.uploadImages : true,
    };
  } catch {
    return defaultDraft;
  }
}

export function NewEncodePage() {
  const [step, setStep] = useState(1);
  const [browsePath, setBrowsePath] = useState<string | undefined>();
  const [folderFilter, setFolderFilter] = useState("");
  const [draft, setDraft] = useState<Draft>(loadDraft);
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  useEffect(() => {
    try {
      localStorage.setItem("bdencode:new-job-draft", JSON.stringify(draft));
    } catch {
      // A böngésző letilthatja vagy megtöltheti a helyi tárhelyet; a varázsló ettől még használható.
    }
  }, [draft]);

  const sources = useQuery({
    queryKey: ["sources", browsePath ?? "root"],
    queryFn: () => api.sources(browsePath),
  });

  const create = useMutation({
    mutationFn: () => api.createJob({
      source_path: draft.sourcePath,
      name: draft.name.trim() || draft.sourceName,
      disc_type: draft.discType,
      content_type: draft.contentType,
      priority: 0,
      settings: {
        detail_level: draft.detailLevel,
        upload_images: draft.uploadImages,
      },
    }),
    onSuccess: (job) => {
      try {
        localStorage.removeItem("bdencode:new-job-draft");
      } catch {
        // A kész munka szerveroldalon már létrejött, a navigáció folytatható.
      }
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      navigate(`/jobs/${job.id}`, { state: { newlyCreated: true } });
    },
  });

  const entries = useMemo(() => {
    const needle = folderFilter.trim().toLocaleLowerCase("hu");
    const values = sources.data?.entries ?? [];
    return needle ? values.filter((entry) => entry.name.toLocaleLowerCase("hu").includes(needle)) : values;
  }, [folderFilter, sources.data]);

  const root = sources.data?.roots.find((value) => (sources.data?.path ?? "").startsWith(value)) ?? sources.data?.roots[0];
  const breadcrumbs = useMemo(() => {
    const current = sources.data?.path;
    if (!current || !root) return [];
    const relative = current.slice(root.length).replace(/^[/\\]+/, "");
    const parts = relative ? relative.split(/[/\\]+/) : [];
    return [{ label: basename(root), path: root }, ...parts.map((part, index) => ({
      label: part,
      path: `${root}/${parts.slice(0, index + 1).join("/")}`,
    }))];
  }, [root, sources.data?.path]);

  function chooseSource(entry: SourceEntry) {
    setDraft((value) => ({
      ...value,
      sourcePath: entry.path,
      sourceName: entry.name,
      name: value.name || entry.name,
    }));
  }

  const stepTitle = step === 1 ? "Forrás kiválasztása" : step === 2 ? "Tartalom megadása" : "Munkamód és ellenőrzés";
  const canContinue = step === 1 ? Boolean(draft.sourcePath) : step === 2 ? Boolean(draft.name.trim()) : true;

  return (
    <div className="page page--wizard">
      <PageHeader
        eyebrow="Új kódolás"
        title={stepTitle}
        description="A lemez először biztonságos, írásmentes scanen megy át. Kódolás csak a későbbi beállítás-jóváhagyás után indul."
      />

      <div className="wizard-steps" aria-label="Lépések">
        {["Forrás", "Tartalom", "Munkamód"].map((label, index) => (
          <button
            type="button"
            key={label}
            className={index + 1 === step ? "wizard-step wizard-step--active" : index + 1 < step ? "wizard-step wizard-step--complete" : "wizard-step"}
            onClick={() => index + 1 < step && setStep(index + 1)}
            disabled={index + 1 > step}
            aria-current={index + 1 === step ? "step" : undefined}
          >
            <span>{index + 1 < step ? <Check size={15} /> : index + 1}</span>
            {label}
          </button>
        ))}
      </div>

      {step === 1 && (
        <Card className="wizard-panel source-browser">
          <div className="source-browser__header">
            <div>
              <span className="eyebrow">Szerver tárhely</span>
              <h2>Válassz BDMV-forrást</h2>
            </div>
            <Button variant="ghost" icon={<RefreshCw size={16} />} onClick={() => void sources.refetch()} loading={sources.isFetching}>Frissítés</Button>
          </div>

          <div className="source-browser__toolbar">
            <nav className="breadcrumbs" aria-label="Mappaútvonal">
              {breadcrumbs.map((item, index) => (
                <button type="button" key={item.path} onClick={() => setBrowsePath(item.path)}>
                  {index === 0 && <HardDriveIcon />}{item.label}
                </button>
              ))}
            </nav>
            <label className="search-field search-field--small">
              <Search size={16} aria-hidden="true" /><input aria-label="Mappa keresése" value={folderFilter} onChange={(event) => setFolderFilter(event.target.value)} placeholder="Mappa keresése…" />
            </label>
          </div>

          {sources.isLoading ? <LoadingPanel label="Mappák beolvasása…" /> : sources.isError ? (
            <Notice tone="danger" title="A tárhely nem olvasható">{sources.error instanceof Error ? sources.error.message : "Ismeretlen kapcsolati hiba"}</Notice>
          ) : (
            <div className="folder-grid">
              {entries.map((entry) => (
                <div key={entry.path} className={draft.sourcePath === entry.path ? "folder-tile folder-tile--selected" : "folder-tile"}>
                  <button type="button" className="folder-tile__open" onClick={() => entry.is_bluray ? chooseSource(entry) : setBrowsePath(entry.path)}>
                    <span className="folder-tile__icon">{entry.is_bluray ? <Disc3 size={25} /> : <Folder size={25} />}</span>
                    <span><strong>{entry.name}</strong><small>{entry.is_bluray ? "Blu-ray forrás" : "Mappa"}</small></span>
                  </button>
                  {entry.is_bluray && (
                    <button type="button" className="folder-tile__select" onClick={() => chooseSource(entry)} aria-label={`${entry.name} kiválasztása`}>
                      {draft.sourcePath === entry.path ? <Check size={17} /> : "Kiválasztás"}
                    </button>
                  )}
                  {!entry.is_bluray && <button type="button" className="folder-tile__chevron" onClick={() => setBrowsePath(entry.path)} aria-label={`${entry.name} megnyitása`}><ArrowRight size={17} aria-hidden="true" /></button>}
                </div>
              ))}
              {!entries.length && <div className="folder-empty"><FolderOpen size={25} /><span>Ebben a mappában nincs további könyvtár.</span></div>}
            </div>
          )}

          {draft.sourcePath && (
            <div className="selected-source">
              <Check size={18} />
              <div><strong>{draft.sourceName}</strong><span>{draft.sourcePath}</span></div>
              <Badge tone="success">Kiválasztva</Badge>
            </div>
          )}
        </Card>
      )}

      {step === 2 && (
        <div className="wizard-content-grid">
          <Card className="wizard-panel">
            <span className="eyebrow">Elnevezés</span>
            <h2>Hogyan jelenjen meg?</h2>
            <label className="field">
              <span>Munka neve</span>
              <input value={draft.name} onChange={(event) => setDraft((value) => ({ ...value, name: event.target.value }))} maxLength={255} placeholder="Például: A film címe (2024)" autoFocus />
              <small>Az MKV végleges fájlnevét a playlist kiválasztásakor még módosíthatod.</small>
            </label>
            <div className="field">
              <span>Lemeztípus</span>
              <div className="segmented-control" role="group" aria-label="Lemeztípus">
                {(["AUTO", "BD", "UHD"] as DiscType[]).map((value) => (
                  <button type="button" key={value} className={draft.discType === value ? "active" : ""} aria-pressed={draft.discType === value} onClick={() => setDraft((draftValue) => ({ ...draftValue, discType: value }))}>
                    {value === "AUTO" ? "Automatikus" : value}
                  </button>
                ))}
              </div>
              <small>Az automatikus felismerés az ajánlott; UHD esetén x265 lesz a kimenet.</small>
            </div>
          </Card>

          <Card className="wizard-panel wizard-panel--wide">
            <span className="eyebrow">Tartalomtípus</span>
            <h2>Mi található a lemezen?</h2>
            <div className="choice-grid choice-grid--content" role="group" aria-label="Tartalomtípus">
              {contentOptions.map(({ value, icon: Icon, title, description }) => (
                <button type="button" key={value} className={draft.contentType === value ? "choice-card choice-card--selected" : "choice-card"} aria-pressed={draft.contentType === value} onClick={() => setDraft((draftValue) => ({ ...draftValue, contentType: value }))}>
                  <span className="choice-card__icon"><Icon size={23} /></span>
                  <span><strong>{title}</strong><small>{description}</small></span>
                  <span className="choice-card__check">{draft.contentType === value && <Check size={15} />}</span>
                </button>
              ))}
            </div>
          </Card>
        </div>
      )}

      {step === 3 && (
        <div className="wizard-content-grid">
          <Card className="wizard-panel wizard-panel--wide">
            <span className="eyebrow">Részletesség</span>
            <h2>Mennyi beállítást szeretnél látni?</h2>
            <div className="choice-grid choice-grid--detail" role="group" aria-label="Beállítások részletessége">
              {detailOptions.map(({ value, icon: Icon, title, description }) => (
                <button type="button" key={value} className={draft.detailLevel === value ? "choice-card choice-card--selected" : "choice-card"} aria-pressed={draft.detailLevel === value} onClick={() => setDraft((draftValue) => ({ ...draftValue, detailLevel: value }))}>
                  <span className="choice-card__icon"><Icon size={23} /></span>
                  <span><strong>{title}</strong><small>{description}</small></span>
                  <span className="choice-card__check">{draft.detailLevel === value && <Check size={15} />}</span>
                </button>
              ))}
            </div>

            <label className="toggle-row">
              <span className="toggle-row__icon"><UploadCloud size={20} /></span>
              <span><strong>Comparison képek feltöltése</strong><small>Az I/P/B framek és spektrumképek ImgBB-re kerülnek, BBCode-dal együtt.</small></span>
              <input type="checkbox" checked={draft.uploadImages} onChange={(event) => setDraft((value) => ({ ...value, uploadImages: event.target.checked }))} />
              <span className="toggle" aria-hidden="true" />
            </label>
          </Card>

          <Card className="review-card">
            <span className="eyebrow">Összegzés</span>
            <h2>Scan indítása</h2>
            <dl className="summary-list">
              <div><dt>Forrás</dt><dd>{draft.sourceName}</dd></div>
              <div><dt>Név</dt><dd>{draft.name}</dd></div>
              <div><dt>Tartalom</dt><dd>{CONTENT_LABELS[draft.contentType]}</dd></div>
              <div><dt>Lemez</dt><dd>{draft.discType === "AUTO" ? "Automatikus felismerés" : draft.discType}</dd></div>
              <div><dt>Nézet</dt><dd>{detailOptions.find((item) => item.value === draft.detailLevel)?.title}</dd></div>
              <div><dt>ImgBB</dt><dd>{draft.uploadImages ? "Bekapcsolva" : "Kikapcsolva"}</dd></div>
            </dl>
            <Notice tone="info">A scan nem módosítja a forrást. A playlistet, sávokat és videóbeállításokat az eredmény után hagyod jóvá.</Notice>
            {create.isError && <Notice tone="danger" title="A munka nem hozható létre">{create.error instanceof ApiError ? create.error.detail : create.error.message}</Notice>}
          </Card>
        </div>
      )}

      <div className="wizard-footer">
        <Button variant="ghost" icon={<ArrowLeft size={17} />} onClick={() => step === 1 ? navigate(-1) : setStep((value) => value - 1)}>
          {step === 1 ? "Mégse" : "Vissza"}
        </Button>
        {step < 3 ? (
          <Button icon={<ArrowRight size={17} />} onClick={() => setStep((value) => value + 1)} disabled={!canContinue}>Tovább</Button>
        ) : (
          <Button icon={<Clapperboard size={18} />} onClick={() => create.mutate()} loading={create.isPending}>Munka létrehozása és scan</Button>
        )}
      </div>
    </div>
  );
}

function HardDriveIcon() {
  return <HardDrive size={14} aria-hidden="true" />;
}

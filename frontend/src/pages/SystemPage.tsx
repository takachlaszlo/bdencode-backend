import { useQuery } from "@tanstack/react-query";
import {
  CheckCircle2,
  Cpu,
  Database,
  Gauge,
  HardDrive,
  RefreshCw,
  Server,
  ShieldCheck,
  Wrench,
  XCircle,
} from "lucide-react";
import { api } from "../api/client";
import { Badge, Button, Card, LoadingPanel, Notice, PageHeader, ProgressBar } from "../components/ui";
import { formatBytes, humanize } from "../utils";

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
}

export function SystemPage() {
  const health = useQuery({ queryKey: ["health"], queryFn: api.health, refetchInterval: 5000 });
  const runtime = useQuery({ queryKey: ["runtime-capabilities"], queryFn: api.runtimeCapabilities, refetchInterval: 60_000 });
  const capabilities = useQuery({ queryKey: ["capabilities"], queryFn: api.capabilities, staleTime: 60_000 });
  const host = record(runtime.data?.host);
  const tools = record(runtime.data?.tools);
  const paths = record(runtime.data?.paths);
  const dataPath = record(paths.data);
  const vapourSynth = record(runtime.data?.vapoursynth);
  const free = typeof dataPath.free_bytes === "number" ? dataPath.free_bytes : 0;
  const total = typeof dataPath.total_bytes === "number" ? dataPath.total_bytes : 0;
  const usedFraction = total ? 1 - free / total : 0;

  function refresh() {
    void Promise.all([health.refetch(), runtime.refetch(), capabilities.refetch()]);
  }

  return (
    <div className="page">
      <PageHeader
        eyebrow="Rendszer"
        title="Szerver és képességek"
        description="Csak olvasható állapotlap a telepített eszközökről és a backend biztonsági korlátairól."
        actions={<Button variant="secondary" icon={<RefreshCw size={17} />} onClick={refresh} loading={health.isFetching || runtime.isFetching}>Frissítés</Button>}
      />
      {(health.isLoading || runtime.isLoading) ? <LoadingPanel label="Rendszeradatok betöltése…" /> : health.isError || runtime.isError ? <Notice tone="danger" title="A rendszerállapot nem olvasható">Az API vagy a runtime-capabilities endpoint nem elérhető.</Notice> : (
        <>
          <div className="system-overview-grid">
            <Card className="status-stat-card"><span className="status-stat-card__icon status-stat-card__icon--green"><Server size={22} /></span><div><small>Backend</small><strong>{health.data?.status === "ok" ? "Online" : "Hiba"}</strong><span>API v{capabilities.data?.api_version ?? "—"}</span></div><CheckCircle2 size={18} className="success-icon" /></Card>
            <Card className="status-stat-card"><span className="status-stat-card__icon"><Cpu size={22} /></span><div><small>Processzor</small><strong>{String(host.logical_cpus ?? "—")} logikai CPU</strong><span>80% teljes keret</span></div><ShieldCheck size={18} /></Card>
            <Card className="status-stat-card"><span className="status-stat-card__icon"><Database size={22} /></span><div><small>Adatbázis</small><strong>Schema {health.data?.schema_version ?? "—"}</strong><span>{health.data?.active_job_id ? "1 aktív munka" : "Üres munkasor"}</span></div><Gauge size={18} /></Card>
            <Card className="status-stat-card"><span className="status-stat-card__icon"><HardDrive size={22} /></span><div><small>Tárhely</small><strong>{formatBytes(free)} szabad</strong><span>{String(dataPath.path ?? "")}</span></div><HardDrive size={18} /></Card>
          </div>

          <div className="system-content-grid">
            <Card className="tools-card">
              <div className="section-heading"><div><span className="section-heading__icon"><Wrench size={19} /></span><div><h2>Telepített programok</h2><p>Az encode és QC tényleges végrehajtói</p></div></div><Badge tone={vapourSynth.ok ? "success" : "danger"}>VapourSynth {vapourSynth.ok ? "OK" : "hiba"}</Badge></div>
              <div className="tool-table">
                {Object.entries(tools).map(([name, raw]) => {
                  const tool = record(raw);
                  const available = Boolean(tool.available);
                  return (
                    <div key={name} className="tool-row">
                      <span className={available ? "tool-row__status tool-row__status--ok" : "tool-row__status tool-row__status--error"}>{available ? <CheckCircle2 size={16} /> : <XCircle size={16} />}</span>
                      <span><strong>{name}</strong><small>{String(tool.version ?? "Nincs verzióadat")}</small></span>
                      <Badge tone={available ? "success" : "danger"}>{available ? "Elérhető" : "Hiányzik"}</Badge>
                    </div>
                  );
                })}
              </div>
            </Card>

            <div className="system-side-stack">
              <Card>
                <span className="eyebrow">Tárhely</span><h2>Encode munkaterület</h2>
                <ProgressBar value={usedFraction} label={`${formatBytes(total - free)} használatban · ${formatBytes(total)} összesen`} />
                <dl className="summary-list summary-list--stacked"><div><dt>Útvonal</dt><dd>{String(dataPath.path ?? "—")}</dd></div><div><dt>Olvasható</dt><dd>{dataPath.readable ? "Igen" : "Nem"}</dd></div><div><dt>Írható</dt><dd>{dataPath.writable ? "Igen" : "Nem"}</dd></div></dl>
              </Card>
              <Card>
                <span className="eyebrow">Biztonsági politika</span><h2>Rögzített korlátok</h2>
                <ul className="policy-list">
                  <li><CheckCircle2 size={16} /> Egyszerre legfeljebb egy aktív encode</li>
                  <li><CheckCircle2 size={16} /> CPU-kapacitás legfeljebb 80%-a</li>
                  <li><CheckCircle2 size={16} /> 3D kimenet tiltva</li>
                  <li><CheckCircle2 size={16} /> Dolby Vision helyett kizárólag HDR10</li>
                  <li><CheckCircle2 size={16} /> Comparison képek veszteségmentes PNG-ben</li>
                </ul>
              </Card>
            </div>
          </div>

          {Array.isArray(runtime.data?.warnings) && runtime.data.warnings.length > 0 && <Notice tone="warning" title="Runtime figyelmeztetések"><ul>{runtime.data.warnings.map((warning) => <li key={String(warning)}>{String(warning)}</li>)}</ul></Notice>}
          <details className="runtime-raw"><summary>{humanize("runtime_capabilities")} JSON</summary><pre>{JSON.stringify(runtime.data, null, 2)}</pre></details>
        </>
      )}
    </div>
  );
}

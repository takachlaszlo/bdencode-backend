import { useQuery } from "@tanstack/react-query";
import {
  CheckCircle2,
  CircleHelp,
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

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function booleanLabel(value: boolean | undefined): string {
  if (value === true) return "Igen";
  if (value === false) return "Nem";
  return "Ismeretlen";
}

export function SystemPage() {
  const health = useQuery({ queryKey: ["health"], queryFn: api.health, refetchInterval: 5000 });
  const runtime = useQuery({ queryKey: ["runtime-capabilities"], queryFn: api.runtimeCapabilities, refetchInterval: 60_000 });
  const capabilities = useQuery({ queryKey: ["capabilities"], queryFn: api.capabilities, staleTime: 60_000 });
  const host = runtime.data?.host;
  const tools = runtime.data?.tools ?? {};
  const dataPath = runtime.data?.paths?.data;
  const free = finiteNumber(dataPath?.free_bytes);
  const total = finiteNumber(dataPath?.total_bytes);
  const hasStorageUsage = free !== null && total !== null;
  const usedFraction = hasStorageUsage && total > 0
    ? Math.max(0, Math.min(1, 1 - free / total))
    : 0;
  const storageLabel = hasStorageUsage
    ? `${formatBytes(Math.max(0, total - free))} használatban · ${formatBytes(total)} összesen`
    : "Tárhelyadatok: Ismeretlen";
  const vapourSynthOk = typeof runtime.data?.vapoursynth?.ok === "boolean"
    ? runtime.data.vapoursynth.ok
    : null;
  const cpuPercentFromRuntime = finiteNumber(runtime.data?.worker_cpu_policy?.requested_percent);
  const cpuFraction = finiteNumber(capabilities.data?.constraints.cpu_budget_fraction);
  const cpuPercent = cpuPercentFromRuntime ?? (cpuFraction === null ? null : cpuFraction * 100);

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
            <Card className="status-stat-card"><span className="status-stat-card__icon status-stat-card__icon--green"><Server size={22} /></span><div><small>Backend</small><strong>{health.data?.status === "ok" ? "Online" : "Hiba"}</strong><span>BDEncode {capabilities.data?.backend_version ?? "—"} · API v{capabilities.data?.api_version ?? "—"}</span></div><CheckCircle2 size={18} className="success-icon" /></Card>
            <Card className="status-stat-card"><span className="status-stat-card__icon"><Cpu size={22} /></span><div><small>Processzor</small><strong>{host?.logical_cpus ?? "Ismeretlen"} logikai CPU</strong><span>{cpuPercent === null ? "CPU-keret: Ismeretlen" : `${Math.round(cpuPercent)}% teljes keret`}</span></div><ShieldCheck size={18} /></Card>
            <Card className="status-stat-card"><span className="status-stat-card__icon"><Database size={22} /></span><div><small>Adatbázis</small><strong>Schema {health.data?.schema_version ?? "—"}</strong><span>{health.data?.active_job_id ? "1 aktív encode" : `${health.data?.ready_jobs ?? 0} kódolásra kész`}{health.data?.preparing_job_id ? " · scan fut" : ""}</span></div><Gauge size={18} /></Card>
            <Card className="status-stat-card"><span className="status-stat-card__icon"><HardDrive size={22} /></span><div><small>Tárhely</small><strong>{free === null ? "Ismeretlen" : `${formatBytes(free)} szabad`}</strong><span>{dataPath?.path || "Ismeretlen"}</span></div><HardDrive size={18} /></Card>
          </div>

          <div className="system-content-grid">
            <Card className="tools-card">
              <div className="section-heading"><div><span className="section-heading__icon"><Wrench size={19} /></span><div><h2>Telepített programok</h2><p>Az encode és QC tényleges végrehajtói</p></div></div><Badge tone={vapourSynthOk === null ? "neutral" : vapourSynthOk ? "success" : "danger"}>VapourSynth {vapourSynthOk === null ? "Ismeretlen" : vapourSynthOk ? "OK" : "hiba"}</Badge></div>
              <div className="tool-table">
                {Object.entries(tools).map(([name, tool]) => {
                  const available = typeof tool.available === "boolean" ? tool.available : null;
                  return (
                    <div key={name} className="tool-row">
                      <span className={available === null ? "tool-row__status" : available ? "tool-row__status tool-row__status--ok" : "tool-row__status tool-row__status--error"}>{available === null ? <CircleHelp size={16} /> : available ? <CheckCircle2 size={16} /> : <XCircle size={16} />}</span>
                      <span><strong>{name}</strong><small>{String(tool.version ?? "Nincs verzióadat")}</small></span>
                      <Badge tone={available === null ? "neutral" : available ? "success" : "danger"}>{available === null ? "Ismeretlen" : available ? "Elérhető" : "Hiányzik"}</Badge>
                    </div>
                  );
                })}
              </div>
            </Card>

            <div className="system-side-stack">
              <Card>
                <span className="eyebrow">Tárhely</span><h2>Encode munkaterület</h2>
                <ProgressBar value={usedFraction} label={storageLabel} />
                <dl className="summary-list summary-list--stacked"><div><dt>Útvonal</dt><dd>{dataPath?.path || "Ismeretlen"}</dd></div><div><dt>Olvasható</dt><dd>{booleanLabel(dataPath?.readable)}</dd></div><div><dt>Írható</dt><dd>{booleanLabel(dataPath?.writable)}</dd></div></dl>
              </Card>
              <Card>
                <span className="eyebrow">Biztonsági politika</span><h2>Rögzített korlátok</h2>
                <ul className="policy-list">
                  <li><CheckCircle2 size={16} /> Egyszerre legfeljebb egy aktív encode</li>
                  <li><CheckCircle2 size={16} /> Scan és beállítás a futó encode mellett is</li>
                  <li><CheckCircle2 size={16} /> CPU-kapacitás legfeljebb 80%-a</li>
                  <li><CheckCircle2 size={16} /> 3D kimenet tiltva</li>
                  <li><CheckCircle2 size={16} /> Dolby Vision helyett kizárólag HDR10</li>
                  <li><CheckCircle2 size={16} /> Comparison képek veszteségmentes PNG-ben</li>
                </ul>
              </Card>
            </div>
          </div>

          {runtime.data?.warnings && runtime.data.warnings.length > 0 && <Notice tone="warning" title="Runtime figyelmeztetések"><ul>{runtime.data.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul></Notice>}
          <details className="runtime-raw"><summary>{humanize("runtime_capabilities")} JSON</summary><pre>{JSON.stringify(runtime.data, null, 2)}</pre></details>
        </>
      )}
    </div>
  );
}

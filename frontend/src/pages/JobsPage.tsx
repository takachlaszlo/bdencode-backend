import { useQuery } from "@tanstack/react-query";
import { Archive, CirclePlus, ListOrdered, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router";
import { api } from "../api/client";
import type { JobState } from "../api/types";
import { JobCard } from "../components/JobCard";
import { EmptyState, LoadingPanel, Notice, PageHeader } from "../components/ui";

const QUEUE_STATES: JobState[] = [
  "QUEUED", "SCANNING", "AWAITING_SELECTION", "READY", "ENCODING", "MUXING", "QC", "COMPARISON", "UPLOADING", "NEEDS_REVIEW", "UPLOAD_FAILED",
];
const ARCHIVE_STATES: JobState[] = ["COMPLETED", "FAILED", "CANCELLED"];

export function JobsPage({ mode }: { mode: "queue" | "archive" }) {
  const [search, setSearch] = useState("");
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

  return (
    <div className="page">
      <PageHeader
        eyebrow={mode === "queue" ? "Munkafolyamat" : "Előzmények"}
        title={mode === "queue" ? "Várólista" : "Elkészült munkák"}
        description={mode === "queue" ? "A rendszer egyszerre egy munkát dolgoz fel; a többi biztonságosan várakozik." : "Kész, hibás és megszakított kódolások visszakereshető mellékletekkel."}
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
      {query.isLoading ? <LoadingPanel label="Munkák betöltése…" /> : filtered.length ? (
        <div className="job-grid">
          {filtered.map((job) => <JobCard key={job.id} job={job} />)}
        </div>
      ) : (
        <EmptyState
          icon={mode === "queue" ? <ListOrdered size={28} /> : <Archive size={28} />}
          title={search ? "Nincs ilyen munka" : mode === "queue" ? "A várólista üres" : "Az archívum még üres"}
          description={search ? "Próbálj más keresőkifejezést." : mode === "queue" ? "Az első forrás hozzáadásával itt jelenik meg a munkafolyamat." : "A lezárt kódolások automatikusan ide kerülnek."}
          action={!search && mode === "queue" ? <Link className="button button--secondary" to="/new">Első munka hozzáadása</Link> : undefined}
        />
      )}
    </div>
  );
}

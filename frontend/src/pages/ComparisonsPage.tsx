import { useQuery } from "@tanstack/react-query";
import { ArrowRight, AudioWaveform, CirclePlus, Images } from "lucide-react";
import { Link } from "react-router";
import { api } from "../api/client";
import type { JobState } from "../api/types";
import { Badge, Card, EmptyState, LoadingPanel, PageHeader } from "../components/ui";
import { formatDate } from "../utils";

const states: JobState[] = ["COMPLETED", "UPLOAD_FAILED"];

export function ComparisonsPage() {
  const jobs = useQuery({ queryKey: ["jobs", "comparisons"], queryFn: () => api.jobs(states, 500), refetchInterval: 15_000 });
  return (
    <div className="page">
      <PageHeader
        eyebrow="Minőség-ellenőrzés"
        title="Összehasonlítások"
        description="I/P/B framepárok, veszteségmentes PNG-k, spektrális hangelemzés és BBCode egy helyen."
      />
      {jobs.isLoading ? <LoadingPanel /> : jobs.data?.items.length ? (
        <div className="comparison-job-grid">
          {jobs.data.items.map((job) => (
            <Card key={job.id} className="comparison-job-card" interactive>
              <div className="comparison-job-card__visual">
                <span><Images size={25} /></span><span><AudioWaveform size={25} /></span>
                <div className="comparison-job-card__frames"><b>I</b><b>P</b><b>B</b></div>
              </div>
              <div className="comparison-job-card__body">
                <div><Badge tone={job.state === "COMPLETED" ? "success" : "warning"}>{job.state === "COMPLETED" ? "Elkészült" : "Feltöltésre vár"}</Badge><small>{formatDate(job.finished_at || job.updated_at)}</small></div>
                <h2>{job.name}</h2>
                <p>Source/encode képpárok és audióspektrumok</p>
                <Link className="text-link" to={`/jobs/${job.id}?tab=comparison`}>Comparison megnyitása <ArrowRight size={15} /></Link>
              </div>
            </Card>
          ))}
        </div>
      ) : (
        <EmptyState icon={<Images size={30} />} title="Még nincs elkészült comparison" description="Minden lezárt encode automatikusan ide kerül az I/P/B és audióelemzésekkel." action={<Link className="button button--secondary" to="/new"><CirclePlus size={17} /> Új kódolás</Link>} />
      )}
    </div>
  );
}

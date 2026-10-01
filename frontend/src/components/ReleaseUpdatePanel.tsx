import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { api } from "../api/client";
import { formatDate } from "../utils";
import { Badge, Card, LoadingPanel, Notice } from "./ui";

type Tone = "success" | "warning" | "danger" | "neutral";

const STATES: Record<string, { label: string; tone: Tone }> = {
  up_to_date: { label: "Naprakész", tone: "success" },
  installed: { label: "Frissítve", tone: "success" },
  update_available: { label: "Új kiadás érhető el", tone: "warning" },
  manual_update_required: { label: "Kézi telepítés kell", tone: "warning" },
  deferred: { label: "Halasztva", tone: "neutral" },
  check_failed: { label: "A keresés nem sikerült", tone: "danger" },
  install_failed: { label: "A telepítés nem sikerült", tone: "danger" },
  blocked: { label: "Leállítva", tone: "danger" },
  invalid_release: { label: "Érvénytelen kiadás", tone: "danger" },
};

/** Outcome of the daily release check and unattended update (System page). */
export function ReleaseUpdatePanel() {
  const query = useQuery({ queryKey: ["release-update"], queryFn: api.releaseUpdate, retry: false });
  const status = query.data?.status ?? null;
  const state = status ? (STATES[status.state] ?? { label: status.state, tone: "neutral" as Tone }) : null;
  return (
    <Card className="backups-panel">
      <div className="section-heading">
        <div>
          <span className="section-heading__icon"><RefreshCw size={19} /></span>
          <div><h3>Kiadáskeresés és frissítés</h3><p>A napi időzítő új kiadást keres, és felügyelet nélkül telepíti</p></div>
        </div>
        {state && <Badge tone={state.tone}>{state.label}</Badge>}
      </div>
      {query.isLoading ? <LoadingPanel label="Frissítési állapot lekérdezése…" /> : status ? (
        <>
          <dl className="summary-list">
            <div><dt>Telepített</dt><dd>{status.installed_version ?? "—"}</dd></div>
            <div><dt>Legújabb kiadás</dt><dd>{status.latest_tag ?? "—"}</dd></div>
            <div><dt>Utolsó ellenőrzés</dt><dd>{formatDate(status.checked_at)}</dd></div>
            <div><dt>Utolsó telepítés</dt><dd>{status.installed_at ? formatDate(status.installed_at) : "—"}</dd></div>
          </dl>
          <p className="muted">{status.message}</p>
          {status.media_updates && status.media_updates.length > 0 && (
            <Notice tone="warning" title="Médiacsomag-frissítés vár (nem települ automatikusan)">
              <ul>{status.media_updates.map((item) => <li key={item}>{item}</li>)}</ul>
            </Notice>
          )}
        </>
      ) : <p className="muted">Még nincs adat: az első napi ellenőrzés után jelenik meg (vagy a frissítő nincs telepítve).</p>}
      <p className="field-help">Kézi indítás: <code>sudo systemctl start bdencode-update.service</code>. Egy adott kiadás (visszaállás régebbire is): <code>sudo /usr/local/libexec/bdencode-release-update install --tag vX.Y.Z</code>.</p>
    </Card>
  );
}

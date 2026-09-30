import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { DatabaseBackup } from "lucide-react";
import { api, ApiError } from "../api/client";
import { formatBytes, formatDate } from "../utils";
import { Badge, Button, Card, LoadingPanel, Notice } from "./ui";

const LABELS: Record<string, string> = {
  scheduled: "Ütemezett",
  manual: "Kézi",
  "pre-migration": "Migráció előtti",
  "pre-restore": "Visszaállítás előtti",
};

function labelText(label: string): string {
  return LABELS[label.replace(/-v\d+$/, "")] ?? label;
}

/** Database health, schema history and verified backups (System page). */
export function BackupsPanel() {
  const queryClient = useQueryClient();
  const status = useQuery({ queryKey: ["database-status"], queryFn: api.databaseStatus, retry: false });
  const backups = useQuery({ queryKey: ["database-backups"], queryFn: api.backups, retry: false });
  const create = useMutation({
    mutationFn: api.createBackup,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["database-backups"] });
      void queryClient.invalidateQueries({ queryKey: ["database-status"] });
    },
  });

  const healthy = status.data?.integrity.length === 1 && status.data.integrity[0] === "ok";
  return (
    <Card className="backups-panel">
      <div className="section-heading">
        <div>
          <span className="section-heading__icon"><DatabaseBackup size={19} /></span>
          <div><h3>Adatbázis és mentések</h3><p>A várólista SQLite adatbázisának állapota és ellenőrzött online mentései</p></div>
        </div>
        <Button icon={<DatabaseBackup size={17} />} loading={create.isPending} onClick={() => create.mutate()}>Mentés most</Button>
      </div>

      {status.isLoading ? <LoadingPanel label="Adatbázis állapotának lekérdezése…" /> : status.data ? (
        <dl className="summary-list">
          <div><dt>Séma</dt><dd>v{status.data.schema_version}</dd></div>
          <div><dt>Méret</dt><dd>{formatBytes(status.data.size_bytes)}</dd></div>
          <div><dt>Integritás</dt><dd><Badge tone={healthy ? "success" : "danger"}>{healthy ? "rendben" : status.data.integrity.join("; ")}</Badge></dd></div>
          <div><dt>Mentések</dt><dd>{status.data.backup_count}</dd></div>
        </dl>
      ) : <Notice tone="warning">Az adatbázis állapota nem olvasható.</Notice>}

      {create.isError && <Notice tone="danger" title="A mentés nem sikerült">{create.error instanceof ApiError ? create.error.detail : "Ismeretlen hiba"}</Notice>}
      {create.isSuccess && <Notice tone="success">A mentés elkészült és ellenőrzött: {create.data.name}</Notice>}

      {backups.data && backups.data.items.length > 0 && (
        <div className="table-scroll">
          <table className="stats-table">
            <thead><tr><th scope="col">Időpont</th><th scope="col">Fajta</th><th scope="col">Méret</th><th scope="col">Séma</th><th scope="col">Munkák</th></tr></thead>
            <tbody>
              {backups.data.items.map((item) => (
                <tr key={item.name}>
                  <th scope="row">{formatDate(item.created_at)}<small>{item.name}</small></th>
                  <td>{labelText(item.label)}</td>
                  <td>{formatBytes(item.size_bytes)}</td>
                  <td>{item.schema_version == null ? "—" : `v${item.schema_version}`}</td>
                  <td>{item.jobs ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {backups.data && backups.data.items.length === 0 && <p className="muted">Még nincs mentés. Az ütemezett mentés a worker üresjáratában készül; a „Mentés most” azonnal ír egyet.</p>}

      {status.data && status.data.migrations.length > 0 && (
        <details className="metric-details">
          <summary>Séma-előzmények</summary>
          <ul className="migration-list">
            {status.data.migrations.map((item) => (
              <li key={item.id}>
                {formatDate(item.applied_at)} · {item.kind === "create" ? `létrehozva v${item.to_version}` : `migráció v${item.from_version} → v${item.to_version}`}
                {item.backup_name ? ` · mentés: ${item.backup_name}` : ""}
                {item.app_version ? ` · BDEncode ${item.app_version}` : ""}
              </li>
            ))}
          </ul>
        </details>
      )}
      <p className="field-help">A visszaállítás szándékosan csak parancssorból lehetséges (<code>bdencode db-restore</code>), leállított szolgáltatásokkal.</p>
    </Card>
  );
}

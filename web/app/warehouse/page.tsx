"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import { ErrorBanner, ScoreTag } from "@/components/Ui";
import type {
  DatasetStatus,
  Instrument,
  Job,
  WarehouseDataset,
  WarehouseFindings,
  WarehouseQueryRows,
} from "@/lib/types";

function StatusTag({ status }: { status: DatasetStatus }) {
  const tone =
    status === "AVAILABLE" ? "good" : status === "QUARANTINED" ? "bad" : "warn";
  return <span className={`tag ${tone}`}>{status.toLowerCase()}</span>;
}

function bytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

/**
 * The row accounting, shown before anything else. A dataset where two thirds of
 * the rows were refused is a different object from one where none were, and no
 * quality score on its own says which you are looking at.
 */
function Conservation({ dataset }: { dataset: WarehouseDataset }) {
  const balanced =
    dataset.rows_in ===
    dataset.rows_written + dataset.rows_excluded + dataset.rows_rejected;
  return (
    <>
      <span className="mono">{dataset.rows_written.toLocaleString()}</span>{" "}
      written
      {dataset.rows_excluded ? ` · ${dataset.rows_excluded} excluded` : ""}
      {dataset.rows_rejected ? ` · ${dataset.rows_rejected} refused` : ""}
      {dataset.rows_flagged ? ` · ${dataset.rows_flagged} flagged` : ""}
      {!balanced ? (
        <span className="tag bad" style={{ marginLeft: 6 }}>
          rows do not balance
        </span>
      ) : null}
    </>
  );
}

function DatasetRow({
  dataset,
  onSelect,
}: {
  dataset: WarehouseDataset;
  onSelect: (dataset: WarehouseDataset) => void;
}) {
  return (
    <tr>
      <td>
        <button className="secondary" onClick={() => onSelect(dataset)}>
          {dataset.name}
        </button>
        <div className="muted" style={{ fontSize: 11 }}>
          {dataset.exchange} · {dataset.kind} · {dataset.layer}
        </div>
      </td>
      <td>
        <StatusTag status={dataset.status} />
      </td>
      <td>
        <Conservation dataset={dataset} />
      </td>
      <td>
        {dataset.quality.overall_score !== null ? (
          <ScoreTag score={dataset.quality.overall_score} />
        ) : (
          <span className="muted">—</span>
        )}
      </td>
      <td className="mono">{dataset.partition_count}</td>
      <td className="mono">{bytes(dataset.bytes_written)}</td>
      <td>
        {dataset.corporate_action_treatment === "UNKNOWN" ? (
          <span className="tag warn">treatment unknown</span>
        ) : (
          <span className="muted">
            {dataset.corporate_action_treatment.toLowerCase().replace(/_/g, " ")}
          </span>
        )}
      </td>
    </tr>
  );
}

export default function WarehousePage() {
  const queryClient = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [exchange, setExchange] = useState("NSE");
  const [instrumentId, setInstrumentId] = useState("");
  const [treatment, setTreatment] = useState("UNADJUSTED");
  const [selected, setSelected] = useState<WarehouseDataset | null>(null);

  const datasets = useQuery({
    queryKey: ["warehouse-datasets"],
    queryFn: () => api.get<WarehouseDataset[]>("/warehouse/datasets"),
  });

  const instruments = useQuery({
    queryKey: ["warehouse-instruments"],
    queryFn: () => api.get<{ items: Instrument[] }>("/instruments?limit=200"),
  });

  const findings = useQuery({
    queryKey: ["warehouse-findings", selected?.id],
    enabled: selected !== null,
    queryFn: () =>
      api.get<WarehouseFindings>(`/warehouse/datasets/${selected!.id}/findings`),
  });

  const preview = useQuery({
    queryKey: ["warehouse-preview", selected?.id],
    enabled: selected !== null && selected.status === "AVAILABLE",
    queryFn: () =>
      api.get<WarehouseQueryRows>(
        `/warehouse/query?exchange=${selected!.exchange}&limit=20`,
      ),
  });

  const ingest = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error("choose a file first");
      const upload = await api.upload<{ id: string }>("/uploads", file, {
        kind: "BARS",
      });
      const job = await api.post<{ job_id: string }>("/warehouse/datasets", {
        upload_id: upload.id,
        name: name || file.name,
        exchange,
        instrument_id: instrumentId || null,
        corporate_action_treatment: treatment,
      });
      // Eager mode finishes inline; queued mode does not, so poll once.
      await api.get<Job>(`/jobs/${job.job_id}`);
      return job;
    },
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["warehouse-datasets"] }),
  });

  return (
    <>
      <h1>Historical warehouse</h1>
      <p className="subtitle">
        Partitioned Parquet in the object store, with a registry of what is in
        it. Nothing here is repaired: a suspicious row is flagged and kept, and a
        dataset validation could not stand behind is quarantined rather than
        served with a warning attached.
      </p>

      <ErrorBanner error={ingest.error ?? datasets.error} />

      <form
        className="card"
        onSubmit={(event) => {
          event.preventDefault();
          ingest.mutate();
        }}
      >
        <h2 style={{ marginTop: 0 }}>Register a dataset</h2>
        <div className="field">
          <label htmlFor="file">CSV or Parquet of OHLCV bars</label>
          <input
            id="file"
            type="file"
            accept=".csv,.parquet,.txt"
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          />
        </div>
        <div className="row">
          <div className="field">
            <label htmlFor="name">Name</label>
            <input
              id="name"
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="exchange">Exchange</label>
            <input
              id="exchange"
              value={exchange}
              onChange={(event) => setExchange(event.target.value)}
            />
          </div>
        </div>
        <div className="field">
          <label htmlFor="instrument">Instrument (optional)</label>
          <select
            id="instrument"
            value={instrumentId}
            onChange={(event) => setInstrumentId(event.target.value)}
          >
            <option value="">Resolve from a symbol column</option>
            {(instruments.data?.items ?? []).map((item) => (
              <option key={item.id} value={item.id}>
                {item.symbol} ({item.exchange})
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="treatment">Corporate actions</label>
          <select
            id="treatment"
            value={treatment}
            onChange={(event) => setTreatment(event.target.value)}
          >
            <option value="UNADJUSTED">Unadjusted — prices as printed</option>
            <option value="ADJUSTED_BY_SOURCE">
              Adjusted by the source
            </option>
            <option value="UNKNOWN">Unknown</option>
          </select>
          <div className="muted" style={{ fontSize: 11 }}>
            Declared, never inferred — this platform holds no corporate-action
            feed. Leaving it unknown means the series cannot safely be joined to
            one whose treatment is known.
          </div>
        </div>
        <button type="submit" disabled={ingest.isPending || !file}>
          {ingest.isPending ? "Reading…" : "Register"}
        </button>
      </form>

      <div className="card">
        <h2 style={{ marginTop: 0 }}>Datasets</h2>
        {datasets.data?.length ? (
          <table>
            <thead>
              <tr>
                <th>Dataset</th>
                <th>Status</th>
                <th>Rows</th>
                <th>Quality</th>
                <th>Files</th>
                <th>Size</th>
                <th>Corporate actions</th>
              </tr>
            </thead>
            <tbody>
              {datasets.data.map((dataset) => (
                <DatasetRow
                  key={dataset.id}
                  dataset={dataset}
                  onSelect={setSelected}
                />
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">Nothing registered yet.</p>
        )}
      </div>

      {selected ? (
        <div className="card">
          <h2 style={{ marginTop: 0 }}>
            {selected.name} <StatusTag status={selected.status} />
          </h2>

          {selected.status === "QUARANTINED" ? (
            <p className="muted">
              Validation found something that makes this unusable as it stands,
              so queries against it are refused. The partitions and the findings
              are both still here — quarantine refuses to serve the data, it does
              not destroy it.
            </p>
          ) : null}

          <table>
            <tbody>
              <tr>
                <td>Completeness</td>
                <td>{selected.quality.completeness_score?.toFixed(3) ?? "—"}</td>
              </tr>
              <tr>
                <td>Consistency</td>
                <td>{selected.quality.consistency_score?.toFixed(3) ?? "—"}</td>
              </tr>
              <tr>
                <td>Outliers</td>
                <td>{selected.quality.outlier_score?.toFixed(3) ?? "—"}</td>
              </tr>
              <tr>
                <td>Provenance</td>
                <td>{selected.quality.source_score?.toFixed(3) ?? "—"}</td>
              </tr>
              <tr>
                <td>Freshness</td>
                <td>
                  {selected.quality.freshness_score?.toFixed(3) ?? (
                    <span className="muted">
                      not measurable — an archive is not stale, it is history
                    </span>
                  )}
                </td>
              </tr>
            </tbody>
          </table>

          {findings.data ? (
            <>
              <h3>What the validator found</h3>
              {findings.data.findings.length ||
              findings.data.rejected.length ||
              findings.data.excluded.length ? (
                <table>
                  <tbody>
                    {[
                      ...findings.data.rejected,
                      ...findings.data.excluded,
                      ...findings.data.findings,
                    ]
                      .slice(0, 50)
                      .map((item, index) => (
                        <tr key={`${item.code}-${index}`}>
                          <td>
                            <span
                              className={`tag ${
                                item.severity === "ERROR"
                                  ? "bad"
                                  : item.severity === "WARNING"
                                    ? "warn"
                                    : "info"
                              }`}
                            >
                              {item.code}
                            </span>
                          </td>
                          <td>
                            {item.message}
                            {item.row_number !== null ? (
                              <div className="muted" style={{ fontSize: 11 }}>
                                row {item.row_number}
                              </div>
                            ) : null}
                          </td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              ) : (
                <p className="muted">Nothing to report.</p>
              )}
            </>
          ) : null}

          {preview.data ? (
            <>
              <h3>First rows</h3>
              <p className="muted">
                Read {preview.data.read_path.toLowerCase()} from{" "}
                {preview.data.partitions_read} partition
                {preview.data.partitions_read === 1 ? "" : "s"}
                {preview.data.truncated ? " · truncated" : ""}
              </p>
              <div style={{ overflowX: "auto" }}>
                <table>
                  <thead>
                    <tr>
                      {preview.data.columns.map((column) => (
                        <th key={column}>{column}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {preview.data.rows.slice(0, 10).map((row, index) => (
                      <tr key={index}>
                        {preview.data!.columns.map((column) => (
                          <td key={column} className="mono">
                            {String(row[column] ?? "—")}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          ) : null}
        </div>
      ) : null}
    </>
  );
}

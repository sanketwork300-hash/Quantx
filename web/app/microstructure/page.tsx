"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { api } from "@/lib/api";
import { Disclaimer, ErrorBanner, SeverityTag } from "@/components/Ui";
import type {
  CapabilityReference,
  DatasetPreview,
  Instrument,
  Job,
  MicrostructureDataset,
} from "@/lib/types";

/** Upload, preview, confirm, import.
 *
 *  The preview step is not a convenience here. The level columns of a wide L2
 *  export are *detected*, and a file whose price and size columns are read the
 *  wrong way round parses cleanly and produces analytics that are wrong in
 *  every number and look entirely ordinary. So the detected mapping is shown
 *  before anything is committed, and the commit sends it back.
 */
export default function MicrostructurePage() {
  const queryClient = useQueryClient();
  const [instrumentId, setInstrumentId] = useState("");
  const [name, setName] = useState("");
  const [snapshotUpload, setSnapshotUpload] = useState<string | null>(null);
  const [eventUpload, setEventUpload] = useState<string | null>(null);
  const [preview, setPreview] = useState<DatasetPreview | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);

  const datasets = useQuery({
    queryKey: ["microstructure-datasets"],
    queryFn: () => api.get<MicrostructureDataset[]>("/microstructure/datasets"),
  });
  const capabilities = useQuery({
    queryKey: ["microstructure-capabilities"],
    queryFn: () => api.get<CapabilityReference[]>("/microstructure/capabilities"),
  });
  const instruments = useQuery({
    queryKey: ["instruments", "all"],
    queryFn: () => api.get<{ items: Instrument[] }>("/instruments?limit=200"),
  });

  const send = useMutation({
    mutationFn: async ({ file, kind }: { file: File; kind: string }) => {
      const created = await api.upload<{ id: string }>("/uploads", file, { kind });
      return { id: created.id, kind };
    },
    onSuccess: ({ id, kind }) => {
      if (kind === "BOOK_SNAPSHOTS") setSnapshotUpload(id);
      else setEventUpload(id);
      setPreview(null);
    },
  });

  const runPreview = useMutation({
    mutationFn: () =>
      api.post<DatasetPreview>("/microstructure/datasets/preview", {
        instrument_id: instrumentId,
        snapshot_upload_id: snapshotUpload,
        event_upload_id: eventUpload,
        limit: 2000,
      }),
    onSuccess: setPreview,
  });

  const commit = useMutation({
    mutationFn: async () => {
      const accepted = await api.post<{ job_id: string }>("/microstructure/datasets", {
        instrument_id: instrumentId,
        name: name || "Untitled dataset",
        snapshot_upload_id: snapshotUpload,
        event_upload_id: eventUpload,
        snapshot_columns: preview?.detected_snapshot_columns ?? null,
        event_mapping: preview?.detected_event_mapping ?? null,
      });
      return accepted.job_id;
    },
    onSuccess: setJobId,
  });

  const job = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api.get<Job>(`/jobs/${jobId}`),
    enabled: jobId !== null,
    refetchInterval: (query) =>
      query.state.data &&
      ["COMPLETED", "FAILED", "CANCELLED"].includes(query.state.data.status)
        ? false
        : 1000,
  });
  if (job.data?.status === "COMPLETED") {
    queryClient.invalidateQueries({ queryKey: ["microstructure-datasets"] });
  }

  return (
    <>
      <h2>Order book</h2>
      <p className="subtitle">
        Depth snapshots and event tapes, and what they can and cannot answer.
        Every analytic here is gated: a dataset is assessed once when it is
        imported, and a capability its data cannot support is refused with a
        reason rather than computed with a caveat. A surface fitted to thin data
        is visibly uncertain; an imbalance computed from a one-level feed is a
        number between -1 and 1 that looks exactly like a real one.
      </p>

      <ErrorBanner
        error={send.error ?? runPreview.error ?? commit.error ?? datasets.error}
      />

      <div className="card">
        <h3 style={{ marginTop: 0 }}>What a feed has to carry</h3>
        <div className="table-wrap" style={{ maxHeight: 300 }}>
          <table>
            <thead>
              <tr>
                <th>Capability</th>
                <th>What it unlocks</th>
                <th>What it needs</th>
              </tr>
            </thead>
            <tbody>
              {(capabilities.data ?? []).map((item) => (
                <tr key={item.capability}>
                  <td className="mono">{item.capability}</td>
                  <td className="muted">{item.measures.join(", ")}</td>
                  <td className="muted">{item.requires}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Import a dataset</h3>
        <div className="row">
          <div className="field" style={{ flex: 1 }}>
            <label htmlFor="inst">Contract</label>
            <select
              id="inst"
              value={instrumentId}
              style={{ width: "100%" }}
              onChange={(event) => setInstrumentId(event.target.value)}
            >
              <option value="">— choose —</option>
              {(instruments.data?.items ?? []).map((item) => (
                <option key={item.id} value={item.id}>
                  {item.canonical_key}
                </option>
              ))}
            </select>
          </div>
          <div className="field" style={{ flex: 1 }}>
            <label htmlFor="name">Name</label>
            <input
              id="name"
              value={name}
              placeholder="NIFTY ATM call, 30 minutes"
              onChange={(event) => setName(event.target.value)}
            />
          </div>
        </div>

        <div className="row">
          <div className="field">
            <label htmlFor="snaps">Depth snapshots (CSV or parquet)</label>
            <input
              id="snaps"
              type="file"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) send.mutate({ file, kind: "BOOK_SNAPSHOTS" });
              }}
            />
            {snapshotUpload && <span className="tag good">uploaded</span>}
          </div>
          <div className="field">
            <label htmlFor="events">Event tape (CSV or parquet)</label>
            <input
              id="events"
              type="file"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) send.mutate({ file, kind: "BOOK_EVENTS" });
              }}
            />
            {eventUpload && <span className="tag good">uploaded</span>}
          </div>
        </div>

        <p className="muted" style={{ marginTop: 0 }}>
          Either half is optional, but they are assessed together: snapshots
          alone support the book measures and nothing that needs the messages
          between them, and a tape alone supports arrival rates and nothing that
          needs a book at an instant. This platform does not reconstruct a book
          from a tape.
        </p>

        <button
          onClick={() => runPreview.mutate()}
          disabled={!instrumentId || (!snapshotUpload && !eventUpload)}
        >
          {runPreview.isPending ? "Reading…" : "Preview"}
        </button>
      </div>

      {preview && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>What was read</h3>
          <p className="muted" style={{ marginTop: 0 }}>
            Check the columns below before importing. A book whose price and
            size columns were read the wrong way round parses without complaint
            and produces analytics that are wrong in every number.
          </p>

          <div className="table-wrap" style={{ maxHeight: 240 }}>
            <table>
              <thead>
                <tr>
                  <th>Level</th>
                  <th>Bid price</th>
                  <th>Bid size</th>
                  <th>Ask price</th>
                  <th>Ask size</th>
                </tr>
              </thead>
              <tbody>
                {Object.keys(preview.detected_snapshot_columns.levels?.bid ?? {})
                  .sort((a, b) => Number(a) - Number(b))
                  .map((level) => {
                    const bid =
                      preview.detected_snapshot_columns.levels.bid?.[level] ?? {};
                    const ask =
                      preview.detected_snapshot_columns.levels.ask?.[level] ?? {};
                    return (
                      <tr key={level}>
                        <td>{level}</td>
                        <td className="mono">{bid.price ?? "—"}</td>
                        <td className="mono">{bid.size ?? "—"}</td>
                        <td className="mono">{ask.price ?? "—"}</td>
                        <td className="mono">{ask.size ?? "—"}</td>
                      </tr>
                    );
                  })}
              </tbody>
            </table>
          </div>

          {preview.detected_snapshot_columns.unrecognised_columns.length > 0 && (
            <p className="muted">
              Not placed, and therefore ignored:{" "}
              <span className="mono">
                {preview.detected_snapshot_columns.unrecognised_columns.join(", ")}
              </span>
            </p>
          )}

          <div className="row">
            <div className="card metric">
              <div className="label">Snapshots kept</div>
              <div className="value">{preview.snapshots.kept}</div>
              <div className="unit">
                of {preview.snapshots.input}, {preview.snapshots.rejected} rejected
              </div>
            </div>
            <div className="card metric">
              <div className="label">Events kept</div>
              <div className="value">{preview.events.kept}</div>
              <div className="unit">
                of {preview.events.input}, {preview.events.rejected} rejected
              </div>
            </div>
          </div>

          <h3>What this dataset would support</h3>
          <ul className="reasons">
            {preview.availability.capabilities.map((verdict) => (
              <li key={verdict.capability}>
                <span className={`tag ${verdict.available ? "good" : "bad"}`}>
                  {verdict.available ? "granted" : "refused"}
                </span>{" "}
                <span className="mono">{verdict.capability}</span>
                <div className="muted">{verdict.message}</div>
              </li>
            ))}
          </ul>

          <button onClick={() => commit.mutate()} disabled={!preview.committable}>
            {commit.isPending ? "Importing…" : "Import"}
          </button>
          {job.data && (
            <p style={{ marginBottom: 0 }}>
              <SeverityTag
                severity={job.data.status === "FAILED" ? "ERROR" : "INFO"}
              />{" "}
              {job.data.status}
            </p>
          )}
        </div>
      )}

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Datasets</h3>
        {datasets.data?.length === 0 && (
          <p className="muted" style={{ marginBottom: 0 }}>
            Nothing imported yet.
          </p>
        )}
        {datasets.data && datasets.data.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Holds</th>
                  <th>Snapshots</th>
                  <th>Events</th>
                  <th>Depth</th>
                  <th>Span</th>
                  <th>Capabilities</th>
                </tr>
              </thead>
              <tbody>
                {datasets.data.map((row) => (
                  <tr key={row.id}>
                    <td>
                      <Link href={`/microstructure/${row.id}`}>{row.name}</Link>
                    </td>
                    <td className="mono">{row.kind}</td>
                    <td>
                      {row.snapshot_rows_kept}
                      {row.snapshot_rows_rejected > 0 && (
                        <span className="muted">
                          {" "}
                          (+{row.snapshot_rows_rejected} rejected)
                        </span>
                      )}
                    </td>
                    <td>
                      {row.event_rows_kept}
                      {row.event_rows_rejected > 0 && (
                        <span className="muted">
                          {" "}
                          (+{row.event_rows_rejected} rejected)
                        </span>
                      )}
                    </td>
                    <td>{row.max_depth_levels}</td>
                    <td>{Math.round(row.span_seconds / 60)} min</td>
                    <td>
                      <span
                        className={`tag ${
                          row.available_capabilities.length === 6 ? "good" : "warn"
                        }`}
                      >
                        {row.available_capabilities.length} / 6
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <Disclaimer />
    </>
  );
}

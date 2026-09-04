"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { api } from "@/lib/api";
import { Disclaimer, ErrorBanner, ScoreTag, SeverityTag } from "@/components/Ui";
import type { DetectedLayout, Job, JobResult, Preview, Upload } from "@/lib/types";

const REQUIRED = ["strike", "option_type", "expiry"];

export default function DataPage() {
  const queryClient = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [upload, setUpload] = useState<Upload | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  // A two-sided chain export is described by column position, not by column
  // name, so it carries its own reading. Held separately from `mapping`
  // because exactly one of the two describes any given file.
  const [layout, setLayout] = useState<DetectedLayout["two_sided"] | null>(null);
  const [expiry, setExpiry] = useState("");
  const [symbol, setSymbol] = useState("NIFTY");
  const [exchange, setExchange] = useState("SYNTH");
  const [asOf, setAsOf] = useState("2026-09-24T09:20");
  const [multiplier, setMultiplier] = useState("");
  const [rate, setRate] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);

  const uploads = useQuery({
    queryKey: ["uploads"],
    queryFn: () => api.get<Upload[]>("/uploads"),
  });

  const doUpload = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error("Choose a CSV file first.");
      const created = await api.upload<Upload>("/uploads", file, {
        kind: "OPTION_CHAIN",
      });
      const previewed = await api.post<Preview>(
        `/uploads/${created.id}/preview`,
        { limit: 20 },
      );
      return { created, previewed };
    },
    onSuccess: ({ created, previewed }) => {
      setUpload(created);
      setPreview(previewed);
      setMapping(previewed.applied_mapping);
      const detected = previewed.detected_layout;
      const twoSided = detected?.layout === "TWO_SIDED" ? detected.two_sided : null;
      setLayout(twoSided);
      setExpiry(twoSided?.expiry ?? detected?.suggested_expiry ?? "");
      if (detected?.suggested_symbol) setSymbol(detected.suggested_symbol);
      queryClient.invalidateQueries({ queryKey: ["uploads"] });
    },
  });

  const ingest = useMutation({
    mutationFn: async () => {
      if (!upload) throw new Error("Upload a file first.");
      const accepted = await api.post<{ job_id: string }>(
        `/uploads/${upload.id}/ingest`,
        {
          kind: "OPTION_CHAIN",
          underlying: { symbol, exchange, asset_class: "INDEX", currency: "INR" },
          as_of_timestamp: new Date(asOf).toISOString(),
          // Exactly one of these describes the file. A two-sided export has to
          // be resolved by column index, because its header names repeat once
          // per side and cannot say which side a column belongs to.
          column_mapping: layout ? {} : mapping,
          layout: layout ? { ...layout, expiry } : null,
          risk_free_rate: rate === "" ? null : Number(rate),
          dividend_yield: rate === "" ? null : 0,
          contract: {
            multiplier: multiplier === "" ? null : multiplier,
            tick_size: "0.05",
            lot_size: "1",
            expiry_time_utc: "10:00:00",
          },
        },
      );
      return accepted.job_id;
    },
    onSuccess: (id) => setJobId(id),
  });

  const job = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api.get<Job>(`/jobs/${jobId}`),
    enabled: jobId !== null,
    refetchInterval: (query) =>
      query.state.data && ["COMPLETED", "FAILED", "CANCELLED"].includes(query.state.data.status)
        ? false
        : 1000,
  });

  const jobResult = useQuery({
    queryKey: ["job-result", jobId],
    queryFn: () => api.get<JobResult>(`/jobs/${jobId}/result`),
    enabled: job.data?.status === "COMPLETED",
  });

  const missing = layout
    ? expiry === ""
      ? ["expiry"]
      : []
    : REQUIRED.filter((field) => !mapping[field]);
  const summary = jobResult.data?.result?.results;

  return (
    <>
      <h2>Data imports</h2>
      <p className="subtitle">
        Upload an option chain, confirm how its columns were read, then ingest.
        Nothing is committed until you have seen the mapping.
      </p>

      <ErrorBanner error={doUpload.error || ingest.error} />

      <div className="card">
        <h3 style={{ marginTop: 0 }}>1. Upload</h3>
        <div className="row">
          <input
            type="file"
            accept=".csv,.txt,.json"
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          />
          <button onClick={() => doUpload.mutate()} disabled={!file || doUpload.isPending}>
            {doUpload.isPending ? "Uploading…" : "Upload and preview"}
          </button>
        </div>
        {upload && (
          <p className="muted" style={{ marginBottom: 0 }}>
            {upload.original_filename} · {upload.byte_size.toLocaleString()} bytes ·
            sha256 <span className="mono">{upload.sha256.slice(0, 16)}…</span>
          </p>
        )}
      </div>

      {preview && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>
            2. Confirm how the file was read
          </h3>
          <p className="muted" style={{ marginTop: 0 }}>
            This reading is a suggestion. A misread column produces a plausible,
            wrong chain and no error at all, so confirm it here.
          </p>

          {missing.length > 0 && (
            <div className="banner warn">
              Required field(s) not mapped: {missing.join(", ")}
            </div>
          )}

          {layout && preview.detected_layout && (
            <>
              <div className="banner">
                Read as a two-sided chain: one row per strike, calls to the left
                of column {layout.strike_column}, puts to the right. Each row
                becomes one call quote and one put quote.
              </div>
              <ul className="muted" style={{ marginTop: 8 }}>
                {preview.detected_layout.evidence.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>

              <div className="field" style={{ maxWidth: 280 }}>
                <label htmlFor="expiry">expiry *</label>
                <input
                  id="expiry"
                  type="date"
                  value={expiry}
                  onChange={(event) => setExpiry(event.target.value)}
                />
                <p className="muted" style={{ marginBottom: 0 }}>
                  {preview.detected_layout.suggestion_source
                    ? `Suggested from the ${preview.detected_layout.suggestion_source}, not from any column in the file. Check it: a wrong expiry moves every contract along the term structure.`
                    : "No expiry column and no filename hint. Supply it."}
                </p>
              </div>

              <div className="table-wrap" style={{ maxHeight: 260 }}>
                <table>
                  <thead>
                    <tr>
                      <th>field</th>
                      <th>call column</th>
                      <th>put column</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <td>strike</td>
                      <td colSpan={2} className="mono">
                        {layout.strike_column} ·{" "}
                        {preview.headers[layout.strike_column]}
                      </td>
                    </tr>
                    {Array.from(
                      new Set([
                        ...Object.keys(layout.call_columns),
                        ...Object.keys(layout.put_columns),
                      ]),
                    )
                      .sort()
                      .map((field) => {
                        const call = layout.call_columns[field];
                        const put = layout.put_columns[field];
                        return (
                          <tr key={field}>
                            <td>{field}</td>
                            <td className="mono">
                              {call === undefined
                                ? "—"
                                : `${call} · ${preview.headers[call]}`}
                            </td>
                            <td className="mono">
                              {put === undefined
                                ? "—"
                                : `${put} · ${preview.headers[put]}`}
                            </td>
                          </tr>
                        );
                      })}
                  </tbody>
                </table>
              </div>
            </>
          )}

          <div className="grid" hidden={layout !== null}>
            {Object.keys(preview.inferred_mapping).length === 0 && (
              <div className="muted">No columns could be inferred.</div>
            )}
            {[
              "strike",
              "option_type",
              "expiry",
              "bid_price",
              "ask_price",
              "last_price",
              "bid_size",
              "ask_size",
              "volume",
              "open_interest",
              "underlying_price",
            ].map((field) => (
              <div className="field" key={field}>
                <label htmlFor={field}>
                  {field}
                  {REQUIRED.includes(field) ? " *" : ""}
                </label>
                <select
                  id={field}
                  value={mapping[field] ?? ""}
                  style={{ width: "100%" }}
                  onChange={(event) => {
                    const next = { ...mapping };
                    if (event.target.value) next[field] = event.target.value;
                    else delete next[field];
                    setMapping(next);
                  }}
                >
                  <option value="">— not mapped —</option>
                  {preview.headers.map((header) => (
                    <option key={header} value={header}>
                      {header}
                    </option>
                  ))}
                </select>
              </div>
            ))}
          </div>

          {preview.unmapped_columns.length > 0 && (
            <p className="muted">
              Ignored columns: {preview.unmapped_columns.join(", ")}. Nothing
              was read from them.
            </p>
          )}

          {preview.parse_errors.length > 0 && (
            <div className="banner warn">
              {preview.parse_errors.length} row(s) in the sample could not be
              parsed. The first: row {preview.parse_errors[0].row_number} —{" "}
              {preview.parse_errors[0].message}
            </div>
          )}

          <h3>Sample as interpreted</h3>
          <div className="table-wrap" style={{ maxHeight: 260 }}>
            <table>
              <thead>
                <tr>
                  {Object.keys(preview.sample_rows[0] ?? {}).map((key) => (
                    <th key={key}>{key}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {preview.sample_rows.slice(0, 10).map((row, index) => (
                  <tr key={index}>
                    {Object.keys(preview.sample_rows[0] ?? {}).map((key) => (
                      <td key={key}>{String(row[key] ?? "—")}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {preview && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>3. Contract and market context</h3>
          <div className="row">
            <div className="field">
              <label htmlFor="symbol">Underlying symbol</label>
              <input id="symbol" value={symbol} onChange={(e) => setSymbol(e.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="exchange">Exchange</label>
              <input id="exchange" value={exchange} onChange={(e) => setExchange(e.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="asof">As-of (UTC)</label>
              <input
                id="asof"
                type="datetime-local"
                value={asOf}
                onChange={(e) => setAsOf(e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="mult">Contract multiplier</label>
              <input
                id="mult"
                placeholder="unknown"
                value={multiplier}
                onChange={(e) => setMultiplier(e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="rate">Risk-free rate</label>
              <input
                id="rate"
                placeholder="unknown"
                value={rate}
                onChange={(e) => setRate(e.target.value)}
              />
            </div>
          </div>
          <p className="muted" style={{ marginTop: 0 }}>
            Leaving the multiplier blank records it as an assumption rather than
            a guess; Greeks and margin scale with it. Leaving the rate blank
            keeps the option bound checks assumption-free, which means
            sub-intrinsic pricing is not checked.
          </p>
          <button onClick={() => ingest.mutate()} disabled={missing.length > 0 || ingest.isPending}>
            {ingest.isPending ? "Submitting…" : "Ingest"}
          </button>
        </div>
      )}

      {job.data && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>4. Job</h3>
          <p>
            <span className="mono">{job.data.job_type}</span>{" "}
            <SeverityTag
              severity={job.data.status === "FAILED" ? "ERROR" : "INFO"}
            />{" "}
            {job.data.status}
          </p>
          <div className="bar">
            <span style={{ width: `${Math.round(job.data.progress * 100)}%` }} />
          </div>
          {job.data.error && (
            <div className="banner error" style={{ marginTop: 12 }}>
              {String(job.data.error.message ?? "Job failed")}
            </div>
          )}
        </div>
      )}

      {summary && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Result</h3>
          <div className="grid">
            <div>
              <div className="muted">Rows in</div>
              <div style={{ fontSize: 20 }}>{summary.counts.input}</div>
            </div>
            <div>
              <div className="muted">Kept</div>
              <div style={{ fontSize: 20 }}>{summary.counts.kept}</div>
            </div>
            <div>
              <div className="muted">Excluded</div>
              <div style={{ fontSize: 20 }}>{summary.counts.excluded}</div>
            </div>
            <div>
              <div className="muted">Rejected</div>
              <div style={{ fontSize: 20 }}>{summary.counts.rejected}</div>
            </div>
            <div>
              <div className="muted">Aggregate quality</div>
              <div style={{ fontSize: 20 }}>
                <ScoreTag score={summary.aggregate_quality.overall_score} />
              </div>
            </div>
          </div>

          {Object.keys(summary.exclusion_counts).length > 0 && (
            <>
              <h3>Why quotes were excluded</h3>
              <ul className="reasons">
                {Object.entries(summary.exclusion_counts).map(([code, count]) => (
                  <li key={code}>
                    <span className="mono">{code}</span> — {count}
                  </li>
                ))}
              </ul>
            </>
          )}

          {Object.keys(summary.rejection_counts).length > 0 && (
            <>
              <h3>Why rows could not become quotes</h3>
              <ul className="reasons">
                {Object.entries(summary.rejection_counts).map(([code, count]) => (
                  <li key={code}>
                    <span className="mono">{code}</span> — {count}
                  </li>
                ))}
              </ul>
            </>
          )}

          <p>
            <Link href={`/markets/chains/${summary.snapshot_id}`}>
              Open the chain snapshot →
            </Link>
          </p>
        </div>
      )}

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Previous uploads</h3>
        {uploads.error && <ErrorBanner error={uploads.error} />}
        <div className="table-wrap" style={{ maxHeight: 260 }}>
          <table>
            <thead>
              <tr>
                <th>File</th>
                <th>Kind</th>
                <th>Bytes</th>
                <th>Status</th>
                <th>Received</th>
              </tr>
            </thead>
            <tbody>
              {(uploads.data ?? []).map((item) => (
                <tr key={item.id}>
                  <td>{item.original_filename}</td>
                  <td>{item.kind}</td>
                  <td>{item.byte_size.toLocaleString()}</td>
                  <td>{item.status}</td>
                  <td>{new Date(item.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <Disclaimer />
    </>
  );
}

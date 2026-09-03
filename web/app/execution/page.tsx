"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { api } from "@/lib/api";
import { Disclaimer, ErrorBanner, Metric, SeverityTag, Warnings } from "@/components/Ui";
import type {
  Envelope,
  ExecutionAnalysisOut,
  ExecutionAnalysisResult,
  ExecutionReportSummary,
  Job,
  JobResult,
  TradeImportPreview,
  Upload,
} from "@/lib/types";

const FIELDS = [
  "timestamp",
  "symbol",
  "side",
  "quantity",
  "price",
  "exchange",
  "asset_class",
  "expiry",
  "strike",
  "option_type",
  "order_id",
  "parent_order",
  "order_type",
  "limit_price",
  "submit_timestamp",
  "decision_timestamp",
  "order_quantity",
  "fees",
  "broker",
];
const REQUIRED = ["timestamp", "symbol", "side", "quantity", "price"];

function money(value: string | number | null | undefined) {
  if (value === null || value === undefined) return "—";
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function useJob(jobId: string | null) {
  return useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api.get<Job>(`/jobs/${jobId}`),
    enabled: jobId !== null,
    refetchInterval: (query) =>
      query.state.data &&
      ["COMPLETED", "FAILED", "CANCELLED"].includes(query.state.data.status)
        ? false
        : 1000,
  });
}

function BenchmarkTable({ report }: { report: ExecutionAnalysisOut }) {
  const shortfalls = new Map(report.shortfalls.map((item) => [item.benchmark, item]));
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Benchmark</th>
            <th>Price</th>
            <th>Shortfall (bps)</th>
            <th>Method</th>
            <th>Window</th>
            <th>Source</th>
            <th>Obs.</th>
          </tr>
        </thead>
        <tbody>
          {report.benchmarks.map((benchmark) => {
            const shortfall = shortfalls.get(benchmark.kind);
            return (
              <tr key={benchmark.kind}>
                <td>
                  {benchmark.kind.replace(/_/g, " ").toLowerCase()}
                  {benchmark.kind === report.primary_benchmark ? (
                    <span className="tag info">primary</span>
                  ) : null}
                </td>
                <td>{benchmark.available ? money(benchmark.price) : "—"}</td>
                <td>
                  {shortfall ? shortfall.basis_points.toFixed(1) : "—"}
                </td>
                <td className="mono">{benchmark.method.toLowerCase()}</td>
                <td className="muted">
                  {benchmark.window.start
                    ? `${new Date(benchmark.window.start).toLocaleTimeString()} – ${
                        benchmark.window.end
                          ? new Date(benchmark.window.end).toLocaleTimeString()
                          : "…"
                      }`
                    : "—"}
                </td>
                <td className="muted">{benchmark.source ?? "—"}</td>
                <td>{benchmark.observations}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {report.benchmarks.some((item) => !item.available) && (
        <ul className="reasons" style={{ marginTop: 12 }}>
          {report.benchmarks
            .filter((item) => !item.available)
            .map((item) => (
              <li key={item.kind}>
                <span className="mono">{item.kind.toLowerCase()}</span> —{" "}
                {item.unavailable_reason}
              </li>
            ))}
        </ul>
      )}
    </div>
  );
}

export default function ExecutionPage() {
  const [file, setFile] = useState<File | null>(null);
  const [upload, setUpload] = useState<Upload | null>(null);
  const [preview, setPreview] = useState<TradeImportPreview | null>(null);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [exchange, setExchange] = useState("SYNTH");
  const [multiplier, setMultiplier] = useState("");
  const [gap, setGap] = useState("300");
  const [importJob, setImportJob] = useState<string | null>(null);
  const [analysisJob, setAnalysisJob] = useState<string | null>(null);

  const reports = useQuery({
    queryKey: ["execution-reports"],
    queryFn: () => api.get<ExecutionReportSummary[]>("/execution/reports"),
  });

  const defaults = () => ({
    exchange,
    currency: "INR",
    multiplier: multiplier === "" ? null : multiplier,
    parent_gap_seconds: Number(gap) || 300,
  });

  const doUpload = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error("Choose a CSV file first.");
      const created = await api.upload<Upload>("/uploads", file, { kind: "TRADES" });
      const previewed = await api.post<TradeImportPreview>("/execution/trades/preview", {
        upload_id: created.id,
        defaults: defaults(),
      });
      return { created, previewed };
    },
    onSuccess: ({ created, previewed }) => {
      setUpload(created);
      setPreview(previewed);
      setMapping(previewed.applied_mapping);
    },
  });

  const commit = useMutation({
    mutationFn: async () => {
      if (!upload) throw new Error("Upload a file first.");
      const accepted = await api.post<{ job_id: string }>("/execution/trades/import", {
        upload_id: upload.id,
        column_mapping: mapping,
        defaults: defaults(),
      });
      return accepted.job_id;
    },
    onSuccess: (id) => setImportJob(id),
  });

  const analyse = useMutation({
    mutationFn: async () => {
      const accepted = await api.post<{ job_id: string }>("/execution/analyze", {
        parent_gap_seconds: Number(gap) || 300,
      });
      return accepted.job_id;
    },
    onSuccess: (id) => setAnalysisJob(id),
  });

  const importStatus = useJob(importJob);
  const analysisStatus = useJob(analysisJob);

  const analysisResult = useQuery({
    queryKey: ["job-result", analysisJob],
    queryFn: async () => {
      const result = await api.get<JobResult>(`/jobs/${analysisJob}/result`);
      reports.refetch();
      return result;
    },
    enabled: analysisStatus.data?.status === "COMPLETED",
  });

  const envelope = analysisResult.data?.result as
    | Envelope<ExecutionAnalysisResult>
    | undefined;
  const analysis = envelope?.results;
  const missing = REQUIRED.filter((field) => !mapping[field]);
  const blocked = !preview || preview.ambiguous.length > 0 || missing.length > 0;

  return (
    <>
      <h2>Execution</h2>
      <p className="subtitle">
        Transaction cost analysis on your own trade log. Every benchmark reports
        the window it covered, where the observations came from and how they were
        combined — and refuses when the data cannot support it.
      </p>

      <ErrorBanner error={doUpload.error ?? commit.error ?? analyse.error} />

      <div className="card">
        <h3 style={{ marginTop: 0 }}>1. Upload a trade log</h3>
        <div className="row">
          <input
            type="file"
            accept=".csv,.txt"
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          />
          <button onClick={() => doUpload.mutate()} disabled={!file || doUpload.isPending}>
            {doUpload.isPending ? "Uploading…" : "Upload and preview"}
          </button>
        </div>
        {upload && (
          <p className="muted" style={{ marginBottom: 0 }}>
            {upload.original_filename} · {upload.byte_size.toLocaleString()} bytes · sha256{" "}
            <span className="mono">{upload.sha256.slice(0, 16)}…</span>
          </p>
        )}
      </div>

      {preview && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>2. Confirm the mapping</h3>
          {missing.length > 0 && (
            <div className="banner warn">
              Required field(s) not mapped: {missing.join(", ")}
            </div>
          )}
          <div className="grid">
            {FIELDS.map((field) => (
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
          <div className="row">
            <div className="field">
              <label htmlFor="exch">Default exchange</label>
              <input id="exch" value={exchange} onChange={(e) => setExchange(e.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="mult">Default multiplier</label>
              <input
                id="mult"
                placeholder="unknown"
                value={multiplier}
                onChange={(e) => setMultiplier(e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="gap">Parent gap (seconds)</label>
              <input id="gap" value={gap} onChange={(e) => setGap(e.target.value)} />
            </div>
          </div>
          <p className="muted" style={{ marginTop: 0 }}>
            The parent gap only affects fills the file did not assign a parent to.
            A different gap produces different parents, different windows and
            different benchmarks, so it is recorded on every report.
          </p>
        </div>
      )}

      {preview && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>3. What the file resolved to</h3>
          <div className="grid">
            <div>
              <div className="muted">Rows read</div>
              <div style={{ fontSize: 20 }}>{preview.rows_in}</div>
            </div>
            <div>
              <div className="muted">Resolved</div>
              <div style={{ fontSize: 20 }}>{preview.resolved.length}</div>
            </div>
            <div>
              <div className="muted">Ambiguous</div>
              <div style={{ fontSize: 20 }}>{preview.ambiguous.length}</div>
            </div>
            <div>
              <div className="muted">Invalid</div>
              <div style={{ fontSize: 20 }}>{preview.invalid.length}</div>
            </div>
          </div>

          {preview.ambiguous.length > 0 && (
            <div className="banner warn" style={{ marginTop: 12 }}>
              {preview.ambiguous.length} row(s) match more than one contract. The
              platform will not pick one for you: a fill on the wrong contract
              lands in the wrong parent order and every cost computed from it is
              wrong with nothing to show for it.
            </div>
          )}

          {preview.invalid.length > 0 && (
            <>
              <h3>Kept out, with the reason</h3>
              <div className="table-wrap" style={{ maxHeight: 240 }}>
                <table>
                  <thead>
                    <tr>
                      <th>Row</th>
                      <th>Reason</th>
                      <th>What is wrong</th>
                    </tr>
                  </thead>
                  <tbody>
                    {preview.invalid.map((row) => (
                      <tr key={`${row.row_number}-${row.reason}`}>
                        <td>{row.row_number}</td>
                        <td className="mono">{row.reason}</td>
                        <td className="muted">{row.message}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}

          <button
            onClick={() => commit.mutate()}
            disabled={blocked || commit.isPending}
            style={{ marginTop: 12 }}
          >
            {commit.isPending ? "Submitting…" : `Import ${preview.resolved.length} fill(s)`}
          </button>
          {importStatus.data && (
            <p style={{ marginBottom: 0 }}>
              <SeverityTag
                severity={importStatus.data.status === "FAILED" ? "ERROR" : "INFO"}
              />{" "}
              {importStatus.data.status}
            </p>
          )}
        </div>
      )}

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Analyse stored fills</h3>
        <p className="muted" style={{ marginTop: 0 }}>
          Groups fills into parent orders and benchmarks each one against arrival,
          decision, prevailing mid, interval TWAP, interval VWAP and close.
        </p>
        <button onClick={() => analyse.mutate()} disabled={analyse.isPending}>
          {analyse.isPending ? "Submitting…" : "Run analysis"}
        </button>
        {analysisStatus.data && (
          <p style={{ marginBottom: 0 }}>
            <SeverityTag
              severity={analysisStatus.data.status === "FAILED" ? "ERROR" : "INFO"}
            />{" "}
            {analysisStatus.data.status}
          </p>
        )}
      </div>

      {envelope?.warnings?.length ? <Warnings warnings={envelope.warnings} /> : null}

      {analysis && (
        <>
          <div className="row">
            <Metric label="Parent orders" value={analysis.parent_orders} />
            <Metric label="Fills analysed" value={analysis.fills} />
            <Metric
              label="Inferred groupings"
              value={
                analysis.reports.filter((r) => r.parent_order.grouping_is_inferred).length
              }
              unit="grouped by the platform, not by the file"
            />
          </div>

          {analysis.reports.map((report) => (
            <div className="card" key={report.parent_order.key}>
              <h3 style={{ marginTop: 0 }}>
                {report.parent_order.symbol ?? "order"} · {report.parent_order.side}{" "}
                {report.parent_order.filled_quantity} @{" "}
                {money(report.parent_order.average_price)}
                {report.parent_order.grouping_is_inferred ? (
                  <span className="tag warn">grouping inferred</span>
                ) : null}
              </h3>
              <p className="muted" style={{ marginTop: 0 }}>
                <span className="mono">{report.parent_order.canonical_key}</span> ·{" "}
                {report.parent_order.fills} fill(s) over{" "}
                {Math.round(report.parent_order.duration_seconds)}s ·{" "}
                {report.market_window.coverage.observations} market observation(s),
                covering {(report.market_window.coverage.span_ratio * 100).toFixed(0)}%
                of the window
                {report.market_window.coverage.is_sufficient ? "" : " (below the threshold for an interval benchmark)"}
              </p>

              <BenchmarkTable report={report} />

              {report.decomposition && (
                <>
                  <h3>Cost decomposition</h3>
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>Component</th>
                          <th>Amount</th>
                          <th>What it is</th>
                          <th>Basis</th>
                        </tr>
                      </thead>
                      <tbody>
                        {report.decomposition.components.map((component) => (
                          <tr key={component.name}>
                            <td className="mono">{component.name}</td>
                            <td>{money(component.amount)}</td>
                            <td>
                              <span
                                className={`tag ${
                                  component.status === "MEASURED"
                                    ? "good"
                                    : component.status === "RESIDUAL"
                                      ? "warn"
                                      : "info"
                                }`}
                              >
                                {component.status.toLowerCase().replace(/_/g, " ")}
                              </span>
                            </td>
                            <td className="muted">{component.basis}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <p className="muted">{report.decomposition.caveat}</p>
                </>
              )}
            </div>
          ))}
        </>
      )}

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Stored reports</h3>
        <div className="table-wrap" style={{ maxHeight: 320 }}>
          <table>
            <thead>
              <tr>
                <th>Parent order</th>
                <th>Side</th>
                <th>Filled</th>
                <th>Average</th>
                <th>Benchmark</th>
                <th>Shortfall (bps)</th>
                <th>Grouping</th>
                <th>Coverage</th>
              </tr>
            </thead>
            <tbody>
              {(reports.data ?? []).map((row) => (
                <tr key={row.id}>
                  <td className="mono">{row.parent_order_key.slice(0, 28)}</td>
                  <td>{row.side}</td>
                  <td>{row.filled_quantity}</td>
                  <td>{money(row.average_price)}</td>
                  <td className="muted">{row.primary_benchmark.toLowerCase()}</td>
                  <td>
                    {row.shortfall_bps === null ? (
                      <span className="muted">no benchmark</span>
                    ) : (
                      row.shortfall_bps.toFixed(1)
                    )}
                  </td>
                  <td>
                    <span
                      className={`tag ${row.grouping_is_inferred ? "warn" : "good"}`}
                    >
                      {row.grouping_is_inferred ? "inferred" : "explicit"}
                    </span>
                  </td>
                  <td>
                    <span
                      className={`tag ${row.coverage_is_sufficient ? "good" : "warn"}`}
                    >
                      {row.observations} obs
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {reports.data?.length === 0 && (
          <p className="muted">Nothing analysed yet.</p>
        )}
      </div>

      <div className="card">
        <p style={{ margin: 0 }}>
          <Link href="/execution/simulate">
            Simulate what a different schedule would have paid →
          </Link>
        </p>
      </div>

      <Disclaimer />
    </>
  );
}

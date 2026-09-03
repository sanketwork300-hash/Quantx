"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api";
import { Disclaimer, ErrorBanner, SeverityTag } from "@/components/Ui";
import type { ImportPreview, Job, Portfolio, Upload } from "@/lib/types";

const FIELDS = [
  "symbol",
  "quantity",
  "exchange",
  "asset_class",
  "side",
  "average_price",
  "expiry",
  "strike",
  "option_type",
  "currency",
  "multiplier",
  "strategy_tag",
];
const REQUIRED = ["symbol", "quantity"];

export default function ImportPositionsPage() {
  const { portfolioId } = useParams<{ portfolioId: string }>();
  const [file, setFile] = useState<File | null>(null);
  const [upload, setUpload] = useState<Upload | null>(null);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [exchange, setExchange] = useState("SYNTH");
  const [currency, setCurrency] = useState("INR");
  const [multiplier, setMultiplier] = useState("");
  const [replaceExisting, setReplaceExisting] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);

  const portfolio = useQuery({
    queryKey: ["portfolio", portfolioId],
    queryFn: () => api.get<Portfolio>(`/portfolios/${portfolioId}`),
  });

  const defaults = () => ({
    currency: currency.toUpperCase(),
    exchange,
    multiplier: multiplier === "" ? null : multiplier,
  });

  const doUpload = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error("Choose a CSV file first.");
      const created = await api.upload<Upload>("/uploads", file, { kind: "POSITIONS" });
      const previewed = await api.post<ImportPreview>(
        `/portfolios/${portfolioId}/import/preview`,
        { upload_id: created.id, defaults: defaults() },
      );
      return { created, previewed };
    },
    onSuccess: ({ created, previewed }) => {
      setUpload(created);
      setPreview(previewed);
      setMapping(previewed.applied_mapping);
    },
  });

  const reprocess = useMutation({
    mutationFn: async () => {
      if (!upload) throw new Error("Upload a file first.");
      return api.post<ImportPreview>(`/portfolios/${portfolioId}/import/preview`, {
        upload_id: upload.id,
        column_mapping: mapping,
        defaults: defaults(),
      });
    },
    onSuccess: (previewed) => setPreview(previewed),
  });

  const commit = useMutation({
    mutationFn: async () => {
      if (!upload) throw new Error("Upload a file first.");
      const accepted = await api.post<{ job_id: string }>(
        `/portfolios/${portfolioId}/import`,
        {
          upload_id: upload.id,
          column_mapping: mapping,
          defaults: defaults(),
          replace_existing: replaceExisting,
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
      query.state.data &&
      ["COMPLETED", "FAILED", "CANCELLED"].includes(query.state.data.status)
        ? false
        : 1000,
  });

  const missing = REQUIRED.filter((field) => !mapping[field]);
  const blocked = !preview || preview.ambiguous.length > 0 || missing.length > 0;

  return (
    <>
      <h2>Import positions</h2>
      <p className="subtitle">
        {portfolio.data ? `Into ${portfolio.data.name}. ` : ""}
        Every row is resolved against the instrument master before anything is
        written, and the import is refused while any row matches more than one
        contract.
      </p>

      <ErrorBanner error={doUpload.error ?? reprocess.error ?? commit.error} />

      <div className="card">
        <h3 style={{ marginTop: 0 }}>1. Upload the position file</h3>
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
          <h3 style={{ marginTop: 0 }}>2. Confirm the mapping and defaults</h3>
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
              <label htmlFor="ccy">Default currency</label>
              <input id="ccy" value={currency} maxLength={3} onChange={(e) => setCurrency(e.target.value)} />
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
          </div>
          <p className="muted">
            A multiplier rescales every value and Greek for a contract. Whether
            you leave it blank or supply one, contracts created by this import
            record it as an assumption rather than a fact from the file.
          </p>
          <button onClick={() => reprocess.mutate()} disabled={reprocess.isPending}>
            {reprocess.isPending ? "Re-reading…" : "Re-read with this mapping"}
          </button>
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
              platform will not pick one for you: correct the file so each row
              names a single contract, then upload it again.
            </div>
          )}

          <h3>Resolved</h3>
          <div className="table-wrap" style={{ maxHeight: 320 }}>
            <table>
              <thead>
                <tr>
                  <th>Row</th>
                  <th>Contract</th>
                  <th>Expiry</th>
                  <th>Strike</th>
                  <th>Type</th>
                  <th>Quantity</th>
                  <th>Side</th>
                  <th>Avg price</th>
                  <th>Tag</th>
                  <th>Resolved by</th>
                </tr>
              </thead>
              <tbody>
                {preview.resolved.map((row) => (
                  <tr key={row.row_number}>
                    <td>{row.row_number}</td>
                    <td className="mono">{row.symbol}</td>
                    <td>{row.expiry ?? "—"}</td>
                    <td>{row.strike ?? "—"}</td>
                    <td>{row.option_type ?? "—"}</td>
                    <td>{row.quantity}</td>
                    <td>{row.side}</td>
                    <td>{row.average_price ?? "—"}</td>
                    <td>{row.strategy_tag ?? "—"}</td>
                    <td className="muted">
                      {row.resolution_method}
                      {row.creates_instrument ? " · creates contract" : ""}
                      {row.multiplier_is_assumed ? " · multiplier assumed" : ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {preview.ambiguous.length > 0 && (
            <>
              <h3>Ambiguous — you must resolve these</h3>
              <div className="table-wrap" style={{ maxHeight: 260 }}>
                <table>
                  <thead>
                    <tr>
                      <th>Row</th>
                      <th>Reason</th>
                      <th>Candidates</th>
                    </tr>
                  </thead>
                  <tbody>
                    {preview.ambiguous.map((row) => (
                      <tr key={row.row_number}>
                        <td>{row.row_number}</td>
                        <td className="mono">{row.reason}</td>
                        <td>
                          {row.candidates.map((candidate) => (
                            <div key={candidate.instrument_id} className="mono">
                              {candidate.canonical_key}
                            </div>
                          ))}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}

          {preview.invalid.length > 0 && (
            <>
              <h3>Invalid — kept out, with the reason</h3>
              <div className="table-wrap" style={{ maxHeight: 260 }}>
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
        </div>
      )}

      {preview && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>4. Commit</h3>
          <label style={{ display: "block", marginBottom: 12 }}>
            <input
              type="checkbox"
              checked={replaceExisting}
              onChange={(event) => setReplaceExisting(event.target.checked)}
            />{" "}
            Replace the existing positions in this portfolio
          </label>
          <button onClick={() => commit.mutate()} disabled={blocked || commit.isPending}>
            {commit.isPending
              ? "Submitting…"
              : `Import ${preview.resolved.length} position(s)`}
          </button>
          {preview.ambiguous.length > 0 && (
            <p className="muted" style={{ marginBottom: 0 }}>
              Blocked while rows are ambiguous.
            </p>
          )}
        </div>
      )}

      {job.data && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Job</h3>
          <p>
            <span className="mono">{job.data.job_type}</span>{" "}
            <SeverityTag severity={job.data.status === "FAILED" ? "ERROR" : "INFO"} />{" "}
            {job.data.status}
          </p>
          {job.data.error && (
            <div className="banner error">{String(job.data.error.message ?? "Job failed")}</div>
          )}
          {job.data.status === "COMPLETED" && (
            <p>
              <Link href={`/portfolios/${portfolioId}`}>Open the portfolio →</Link>
            </p>
          )}
        </div>
      )}

      <Disclaimer />
    </>
  );
}

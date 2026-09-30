"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Disclaimer, ErrorBanner, ScoreTag, SeverityTag, Warnings } from "@/components/Ui";
import type {
  DateOrder,
  FieldReading,
  Job,
  JobResult,
  Preview,
  ReadingSource,
  SampleRow,
  TwoSidedLayout,
  Upload,
} from "@/lib/types";

/** Fields a two-sided export carries once per side, and which side owns them. */
const SIDED_FIELDS = [
  "bid_price",
  "ask_price",
  "last_price",
  "bid_size",
  "ask_size",
  "volume",
  "open_interest",
];

const LONG_FIELDS = [
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
];

const SOURCE_LABEL: Record<ReadingSource, { text: string; tone: string }> = {
  DETECTED_COLUMN: { text: "detected", tone: "info" },
  SUPPLIED_COLUMN: { text: "you set this", tone: "good" },
  IMPLIED_BY_POSITION: { text: "by position", tone: "info" },
  STATED_SEPARATELY: { text: "not a column", tone: "warn" },
  NOT_IN_FILE: { text: "not in this file", tone: "warn" },
};

function readFrom(item: FieldReading): string {
  if (item.columns.length === 0) return "—";
  return item.columns
    .map((column) => {
      const where = column.index === null ? "" : ` (col ${column.index})`;
      const name = `${column.header ?? "?"}${where}`;
      return column.side === "BOTH" ? name : `${column.side.toLowerCase()}: ${name}`;
    })
    .join(" · ");
}

/** The current instant in the shape a `datetime-local` input wants. */
function nowForInput(): string {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

/** Distinct expiries the sample actually carries, so the as-of can be checked. */
function sampleExpiries(sample: SampleRow[]): string[] {
  const seen = new Set<string>();
  for (const row of sample) {
    const value = row.values.expiry;
    if (value) seen.add(value);
  }
  return [...seen].sort();
}

/** Fields that carry a value in at least one sampled row, in reading order. */
function sampleColumns(reading: FieldReading[], sample: SampleRow[]): string[] {
  const present = new Set<string>();
  for (const row of sample) {
    for (const [field, value] of Object.entries(row.values)) {
      if (value !== null && value !== undefined) present.add(field);
    }
  }
  return reading.map((item) => item.field).filter((field) => present.has(field));
}

export default function DataPage() {
  const queryClient = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [upload, setUpload] = useState<Upload | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  // Corrections the user made. Empty until they change something: an untouched
  // reading is sent as nothing at all, so the server reports every column as
  // its own reading rather than as the user's instruction.
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [layout, setLayout] = useState<TwoSidedLayout | null>(null);
  const [corrected, setCorrected] = useState(false);
  const [editing, setEditing] = useState(false);
  const [expiry, setExpiry] = useState("");
  const [symbol, setSymbol] = useState("NIFTY");
  const [exchange, setExchange] = useState("SYNTH");
  // Set on mount rather than at first render: the value differs between the
  // server render and the client, and a fixed default is worse than either --
  // an as-of past the chain's expiry makes every quote OPTION_EXPIRED and
  // nothing downstream can be solved from it.
  const [asOf, setAsOf] = useState("");
  const [multiplier, setMultiplier] = useState("");
  const [rate, setRate] = useState("");
  // Shown and editable rather than sent unseen: it decides which contracts
  // count as expired. Blank leaves the expiry instant unknown.
  const [settlement, setSettlement] = useState("10:00");
  // Blank until the user says: a date like 05/10/2026 is never read one way on
  // their behalf. Sent only when set.
  const [dateOrder, setDateOrder] = useState<DateOrder | "">("");
  const [jobId, setJobId] = useState<string | null>(null);

  useEffect(() => setAsOf(nowForInput()), []);

  const uploads = useQuery({
    queryKey: ["uploads"],
    queryFn: () => api.get<Upload[]>("/uploads"),
  });

  /**
   * Take on a fresh reading. `applied_mapping` is what the server actually read
   * with — the detected mapping, or the user's correction where they made one —
   * so it is always the right thing to show in the editor.
   */
  function absorb(seen: Preview, options: { newFile?: boolean } = {}) {
    setPreview(seen);
    const detected = seen.detected_layout;
    const twoSided = detected?.layout === "TWO_SIDED" ? detected.two_sided : null;
    setLayout(twoSided);
    setMapping(seen.applied_mapping);
    // The expiry of a two-sided export is not in the file. It is taken from the
    // new file's own suggestion, and never carried over from the last one.
    if (options.newFile) setExpiry(twoSided?.expiry ?? detected?.suggested_expiry ?? "");
    if (detected?.suggested_symbol) setSymbol(detected.suggested_symbol);
  }

  const doUpload = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error("Choose a CSV file first.");
      const created = await api.upload<Upload>("/uploads", file, {
        kind: "OPTION_CHAIN",
      });
      const previewed = await api.post<Preview>(`/uploads/${created.id}/preview`, {
        limit: 25,
      });
      return { created, previewed };
    },
    onSuccess: ({ created, previewed }) => {
      setUpload(created);
      setCorrected(false);
      setEditing(false);
      setJobId(null);
      setDateOrder("");
      absorb(previewed, { newFile: true });
      queryClient.invalidateQueries({ queryKey: ["uploads"] });
    },
  });

  /** Re-read the file with the corrections applied, so the user sees the effect. */
  const reread = useMutation({
    mutationFn: async (next: {
      mapping?: Record<string, string>;
      layout?: TwoSidedLayout | null;
      dateOrder?: DateOrder | "";
    }) => {
      if (!upload) throw new Error("Upload a file first.");
      const order = next.dateOrder ?? dateOrder;
      const body: Record<string, unknown> = { limit: 25, date_order: order === "" ? null : order };
      if (next.layout) body.layout = { ...next.layout, expiry: expiry || next.layout.expiry };
      else if (next.mapping) body.column_mapping = next.mapping;
      return api.post<Preview>(`/uploads/${upload.id}/preview`, body);
    },
    onSuccess: (seen) => absorb(seen),
  });

  const ingest = useMutation({
    mutationFn: async () => {
      if (!upload) throw new Error("Upload a file first.");
      const accepted = await api.post<{ job_id: string }>(`/uploads/${upload.id}/ingest`, {
        kind: "OPTION_CHAIN",
        underlying: { symbol, exchange, asset_class: "INDEX", currency: "INR" },
        as_of_timestamp: new Date(asOf).toISOString(),
        // Exactly one of these describes the file. A two-sided export has to be
        // resolved by column index, because its header names repeat once per
        // side and cannot say which side a column belongs to.
        column_mapping: layout ? {} : corrected ? mapping : {},
        layout: layout ? { ...layout, expiry } : null,
        date_order: dateOrder === "" ? null : dateOrder,
        risk_free_rate: rate === "" ? null : Number(rate),
        dividend_yield: rate === "" ? null : 0,
        contract: {
          multiplier: multiplier === "" ? null : multiplier,
          tick_size: "0.05",
          lot_size: "1",
          expiry_time_utc: settlement === "" ? null : `${settlement}:00`,
        },
      });
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

  const verdict = preview?.verdict ?? null;
  const needsExpiry = layout !== null && expiry === "";
  const canIngest = verdict !== null && verdict.readable && !needsExpiry && asOf !== "";
  // An as-of at or after an expiry makes every quote of that expiry
  // OPTION_EXPIRED: it stores, and then no implied volatility, surface or scan
  // can be solved from it. Said here rather than discovered two screens later.
  const expiries = preview ? sampleExpiries(preview.sample) : [];
  const chainExpiry = layout ? (expiry === "" ? null : expiry) : (expiries[0] ?? null);
  // Judged the way the server judges it: by the settlement instant when one is
  // given, and otherwise by the date alone, because the instant is then unknown.
  const asOfInstant = asOf === "" ? null : new Date(asOf);
  const asOfPastExpiry =
    chainExpiry !== null &&
    asOfInstant !== null &&
    !Number.isNaN(asOfInstant.getTime()) &&
    (settlement !== ""
      ? asOfInstant.getTime() >= new Date(`${chainExpiry}T${settlement}:00Z`).getTime()
      : asOfInstant.toISOString().slice(0, 10) > chainExpiry);
  const summary = jobResult.data?.result?.results;
  const columns = preview ? sampleColumns(preview.reading, preview.sample) : [];

  function correctLong(field: string, column: string) {
    const next = { ...mapping };
    if (column) next[field] = column;
    else delete next[field];
    setMapping(next);
    setCorrected(true);
    reread.mutate({ mapping: next });
  }

  function correctSided(field: string, side: "call_columns" | "put_columns", value: string) {
    if (!layout) return;
    const assigned = { ...layout[side] };
    if (value === "") delete assigned[field];
    else assigned[field] = Number(value);
    const next = { ...layout, [side]: assigned };
    setLayout(next);
    setCorrected(true);
    reread.mutate({ layout: next });
  }

  function stateDateOrder(value: DateOrder | "") {
    setDateOrder(value);
    reread.mutate({ mapping: corrected && !layout ? mapping : undefined, dateOrder: value });
  }

  function resetReading() {
    setCorrected(false);
    setMapping({});
    reread.mutate({});
  }

  return (
    <>
      <h1>Data imports</h1>
      <p className="subtitle">
        Upload an option chain. The file is read for you; what follows is what
        was read, column by column, with the rows it could not read shown rather
        than skipped. Correct it if a column was taken for the wrong field.
      </p>

      <ErrorBanner error={doUpload.error || reread.error || ingest.error} />

      <div className="card">
        <h2 style={{ marginTop: 0 }}>1. Upload</h2>
        <div className="row">
          <input
            type="file"
            accept=".csv,.txt,.json"
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          />
          <button onClick={() => doUpload.mutate()} disabled={!file || doUpload.isPending}>
            {doUpload.isPending ? "Reading…" : "Upload and read"}
          </button>
        </div>
        {upload && (
          <p className="muted" style={{ marginBottom: 0 }}>
            {upload.original_filename} · {upload.byte_size.toLocaleString()} bytes ·
            sha256 <span className="mono">{upload.sha256.slice(0, 16)}…</span>
          </p>
        )}
      </div>

      {preview && verdict && (
        <div className="card">
          <h2 style={{ marginTop: 0 }}>2. How the file was read</h2>

          {verdict.readable ? (
            <div className="banner note">
              {verdict.rows_read} of the {verdict.rows_examined} quote(s) in the first{" "}
              {verdict.source_rows ?? verdict.rows_examined} row(s) were read.
              {verdict.rows_unreadable > 0 &&
                ` ${verdict.rows_unreadable} could not be read at all.`}
              {verdict.rows_empty > 0 &&
                ` ${verdict.rows_empty} carried no price on that side, which an exchange chain does at far strikes.`}
              {verdict.message && ` ${verdict.message}`}
            </div>
          ) : (
            <div className="banner error">
              <strong>{verdict.problem}</strong> — {verdict.message}
            </div>
          )}

          {layout && preview.detected_layout && (
            <>
              <div className="banner">
                Read as a two-sided chain: one row per strike, with the strike
                in column {layout.strike_column}. Each row becomes one call quote
                and one put quote; how the sides were told apart is listed below.
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
            </>
          )}

          {!layout && preview.detected_layout && preview.detected_layout.evidence.length > 1 && (
            <ul className="muted" style={{ marginTop: 8 }}>
              {preview.detected_layout.evidence.slice(1).map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          )}

          {preview.date_readings.length > 0 && (
            <div className="field" style={{ maxWidth: 520 }}>
              <label htmlFor="dateorder">Date order</label>
              <select
                id="dateorder"
                value={dateOrder}
                onChange={(event) => stateDateOrder(event.target.value as DateOrder | "")}
              >
                <option value="">— as the file shows —</option>
                <option value="DMY">day first (dd/mm/yyyy)</option>
                <option value="MDY">month first (mm/dd/yyyy)</option>
              </select>
              {preview.date_readings.map((item) => (
                <p className="muted" style={{ marginBottom: 0 }} key={item.column}>
                  {item.problem === "AMBIGUOUS"
                    ? `Column ${item.column} writes dates like ${item.example}, which read day-first or month-first, and no value in it says which. Choose one; it is not guessed.`
                    : item.problem === "CONFLICTING"
                      ? `Column ${item.column} holds dates that can only be day-first and dates that can only be month-first (${item.example}). Choose one; values that do not fit are refused by row.`
                      : item.stated
                        ? `Column ${item.column} is read ${item.order === "DMY" ? "day first" : "month first"}, as you set.`
                        : `Column ${item.column} is read ${item.order === "DMY" ? "day first" : "month first"}, because ${item.example} reads no other way.`}
                </p>
              ))}
            </div>
          )}

          <div className="table-wrap" style={{ maxHeight: 300 }}>
            <table>
              <thead>
                <tr>
                  <th>Field</th>
                  <th>Read from</th>
                  <th>Where that came from</th>
                </tr>
              </thead>
              <tbody>
                {preview.reading.map((item) => (
                  <tr key={item.field}>
                    <td>
                      {item.field}
                      {item.required ? " *" : ""}
                    </td>
                    <td className="mono">{readFrom(item)}</td>
                    <td>
                      <span className={`tag ${SOURCE_LABEL[item.source].tone}`}>
                        {SOURCE_LABEL[item.source].text}
                      </span>
                      {item.detail && (
                        <div className="muted" style={{ fontSize: 11, marginTop: 3 }}>
                          {item.detail}
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="row" style={{ marginTop: 12 }}>
            <button onClick={() => setEditing(!editing)} disabled={reread.isPending}>
              {editing ? "Done correcting" : "Correct a column"}
            </button>
            {corrected && (
              <button onClick={resetReading} disabled={reread.isPending}>
                Reset to what was detected
              </button>
            )}
            {reread.isPending && <span className="muted">Re-reading the file…</span>}
          </div>

          {editing && !layout && (
            <div className="grid" style={{ marginTop: 12 }}>
              {LONG_FIELDS.map((field) => (
                <div className="field" key={field}>
                  <label htmlFor={field}>{field}</label>
                  <select
                    id={field}
                    value={mapping[field] ?? ""}
                    style={{ width: "100%" }}
                    onChange={(event) => correctLong(field, event.target.value)}
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
          )}

          {editing && layout && (
            <div className="table-wrap" style={{ marginTop: 12, maxHeight: 320 }}>
              <table>
                <thead>
                  <tr>
                    <th>Field</th>
                    <th>Call column</th>
                    <th>Put column</th>
                  </tr>
                </thead>
                <tbody>
                  {SIDED_FIELDS.map((field) => (
                    <tr key={field}>
                      <td>{field}</td>
                      {(["call_columns", "put_columns"] as const).map((side) => (
                        <td key={side}>
                          <select
                            value={layout[side][field] ?? ""}
                            onChange={(event) => correctSided(field, side, event.target.value)}
                          >
                            <option value="">— not mapped —</option>
                            {preview.headers.map((header, index) => (
                              <option key={`${header}-${index}`} value={index}>
                                {index} · {header}
                              </option>
                            ))}
                          </select>
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {preview.unmapped_columns.length > 0 && (
            <p className="muted">
              Ignored columns: {preview.unmapped_columns.join(", ")}. Nothing was
              read from them.
            </p>
          )}

          <h2>The first rows, as they were read</h2>
          <p className="muted" style={{ marginTop: 0 }}>
            Only the columns that were mapped are shown, and a row that could not
            be read is shown with its reason rather than left out.
          </p>
          <div className="table-wrap" style={{ maxHeight: 320 }}>
            <table>
              <thead>
                <tr>
                  <th>Row</th>
                  {columns.map((field) => (
                    <th key={field}>{field}</th>
                  ))}
                  <th>Problem</th>
                </tr>
              </thead>
              <tbody>
                {preview.sample.map((row, index) => (
                  <tr key={`${row.row_number}-${index}`}>
                    <td className="mono">{row.row_number}</td>
                    {columns.map((field) => (
                      <td key={field}>{row.values[field] ?? "—"}</td>
                    ))}
                    <td className={row.read ? "muted" : ""}>
                      {row.read ? (
                        "—"
                      ) : (
                        <>
                          <span className={`tag ${row.structural ? "bad" : "warn"}`}>
                            {row.reason}
                          </span>{" "}
                          <span className="muted">{row.problem}</span>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {preview && (
        <div className="card">
          <h2 style={{ marginTop: 0 }}>3. Contract and market context</h2>
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
              <label htmlFor="settlement">Settlement time on expiry day (UTC)</label>
              <input
                id="settlement"
                type="time"
                value={settlement}
                onChange={(e) => setSettlement(e.target.value)}
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
            sub-intrinsic pricing is not checked. The settlement time is when a
            contract stops being live on its expiry date: 10:00 UTC is 15:30
            IST, and is a starting value, not something read from the file.
            Clear it if you do not know it; the expiry instant is then recorded
            as unknown and time to expiry is undefined.
          </p>
          {expiries.length > 0 && (
            <p className="muted" style={{ marginTop: 0 }}>
              Expiries in this file: {expiries.join(", ")}. The as-of is yours,
              not the file&apos;s — nothing in a chain export states when it was
              captured.
            </p>
          )}
          {asOfPastExpiry && (
            <div className="banner warn">
              The as-of is past the {chainExpiry} expiry
              {settlement !== "" ? ` (settlement ${settlement} UTC)` : ""}, so those
              contracts had already expired at that instant. No implied
              volatility, surface or deviation scan can be solved from them, and
              if every contract in the file has expired the ingest is refused.
              Set the as-of to when the chain was captured.
            </div>
          )}
          {!canIngest && (
            <div className="banner warn">
              {needsExpiry
                ? "Supply the expiry above before ingesting."
                : verdict?.problem === "AMBIGUOUS_DATE_ORDER"
                  ? "Choose the date order above before ingesting."
                  : "This file cannot be ingested as it is being read. Correct the columns above."}
            </div>
          )}
          <button onClick={() => ingest.mutate()} disabled={!canIngest || ingest.isPending}>
            {ingest.isPending ? "Submitting…" : "Ingest"}
          </button>
        </div>
      )}

      {job.data && (
        <div className="card">
          <h2 style={{ marginTop: 0 }}>4. Job</h2>
          <p>
            <span className="mono">{job.data.job_type}</span>{" "}
            <SeverityTag severity={job.data.status === "FAILED" ? "ERROR" : "INFO"} />{" "}
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
          {job.data.error?.details != null && (
            <p className="muted" style={{ marginBottom: 0 }}>
              Nothing was written. The whole file was read and refused, so no
              snapshot exists that would look like a quiet market.
            </p>
          )}
        </div>
      )}

      {summary && (
        <div className="card">
          <h2 style={{ marginTop: 0 }}>Result</h2>
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

          {jobResult.data?.result && (
            <Warnings warnings={jobResult.data.result.warnings} />
          )}

          {Object.keys(summary.exclusion_counts).length > 0 && (
            <>
              <h2>Why quotes were excluded</h2>
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
              <h2>Why rows could not become quotes</h2>
              <ul className="reasons">
                {Object.entries(summary.rejection_counts).map(([code, count]) => (
                  <li key={code}>
                    <span className="mono">{code}</span> — {count}
                  </li>
                ))}
              </ul>
            </>
          )}

          <h2>What this chain now supports</h2>
          <div className="row">
            <Link className="button" href={`/markets/chains/${summary.snapshot_id}`}>
              Chain snapshot →
            </Link>
            <Link className="button" href={`/markets/chains/${summary.snapshot_id}/smile`}>
              Implied volatility →
            </Link>
            <Link className="button" href={`/markets/chains/${summary.snapshot_id}/surface`}>
              Surface and arbitrage →
            </Link>
            <Link className="button" href={`/markets/chains/${summary.snapshot_id}/scanner`}>
              Surface deviations →
            </Link>
          </div>
        </div>
      )}

      <div className="card">
        <h2 style={{ marginTop: 0 }}>Previous uploads</h2>
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

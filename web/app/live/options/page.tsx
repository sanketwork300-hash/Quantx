"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import { Disclaimer, ErrorBanner, Warnings } from "@/components/Ui";
import type {
  ExpiryOpenInterest,
  Instrument,
  Job,
  JobResult,
  LiveOptionsResult,
  OpenInterestProfile,
  SliceDeltaSkew,
} from "@/lib/types";

function useJob(jobId: string | null) {
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
  const result = useQuery({
    queryKey: ["job-result", jobId],
    queryFn: () => api.get<JobResult>(`/jobs/${jobId}/result`),
    enabled: job.data?.status === "COMPLETED",
  });
  return { job, result };
}

function num(value: number | null | undefined, digits = 4) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function vol(value: number | null | undefined) {
  if (value === null || value === undefined) return "—";
  return `${(value * 100).toFixed(2)}%`;
}

function count(value: string | null) {
  if (value === null) return "—";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toLocaleString() : value;
}

/**
 * The capture's own account of itself. Shown before any analytics, because a
 * surface fitted from a chain that was two-thirds missing is a different object
 * from one fitted from a full chain, and nothing further down says so.
 */
function CaptureSummary({ result }: { result: LiveOptionsResult }) {
  const capture = result.capture;
  return (
    <div className="card">
      <h2 style={{ marginTop: 0 }}>What was captured</h2>
      <table>
        <tbody>
          <tr>
            <td>Contracts considered</td>
            <td className="mono">{capture.contracts_considered}</td>
          </tr>
          <tr>
            <td>Quotes kept</td>
            <td className="mono">{capture.quotes_kept}</td>
          </tr>
          <tr>
            <td>Excluded by quality</td>
            <td className="mono">{capture.quotes_excluded}</td>
          </tr>
          <tr>
            <td>No live price</td>
            <td className="mono">{capture.contracts_without_quotes}</td>
          </tr>
          <tr>
            <td>Underlying level</td>
            <td className="mono">{capture.underlying_price ?? "—"}</td>
          </tr>
          <tr>
            <td>Quotes span</td>
            <td>
              <span className="mono">
                {capture.timestamp_spread_seconds.toFixed(0)}s
              </span>
              <div className="muted" style={{ fontSize: 11 }}>
                How much of an instant this snapshot is. Every calibration below
                treats these quotes as simultaneous.
              </div>
            </td>
          </tr>
        </tbody>
      </table>
      {!capture.conserved ? (
        <p className="tag bad">
          Rows do not balance — this is a bug, not a data problem.
        </p>
      ) : null}
    </div>
  );
}

/** Skew in the units a market quotes it in, with the convention on the label. */
function DeltaSkewTable({ slices }: { slices: SliceDeltaSkew[] }) {
  return (
    <table>
      <thead>
        <tr>
          <th>Expiry</th>
          <th>ATM</th>
          <th>25Δ RR</th>
          <th>25Δ BF</th>
          <th>10Δ RR</th>
          <th>10Δ BF</th>
        </tr>
      </thead>
      <tbody>
        {slices.map((item) => {
          const near = item.smiles.find((s) => s.delta_level === 0.25);
          const far = item.smiles.find((s) => s.delta_level === 0.1);
          return (
            <tr key={item.expiry}>
              <td>
                {item.expiry}
                {item.degraded ? (
                  <span className="tag warn" style={{ marginLeft: 6 }}>
                    degraded fit
                  </span>
                ) : null}
              </td>
              <td className="mono">{vol(item.atm_volatility)}</td>
              {/* A null is a wing that could not be found, not a skew of zero. */}
              <td className="mono">{vol(near?.risk_reversal)}</td>
              <td className="mono">{vol(near?.butterfly)}</td>
              <td className="mono">{vol(far?.risk_reversal)}</td>
              <td className="mono">{vol(far?.butterfly)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function OpenInterestTable({ expiries }: { expiries: ExpiryOpenInterest[] }) {
  return (
    <table>
      <thead>
        <tr>
          <th>Expiry</th>
          <th>Call OI</th>
          <th>Put OI</th>
          <th>PCR (OI)</th>
          <th>PCR (volume)</th>
          <th>Volume / OI</th>
          <th>Coverage</th>
        </tr>
      </thead>
      <tbody>
        {expiries.map((item) => (
          <tr key={item.expiry}>
            <td>{item.expiry}</td>
            <td className="mono">{count(item.call_open_interest)}</td>
            <td className="mono">{count(item.put_open_interest)}</td>
            <td className="mono">{num(item.put_call_ratio_open_interest, 3)}</td>
            <td className="mono">{num(item.put_call_ratio_volume, 3)}</td>
            <td className="mono">{num(item.volume_to_open_interest, 3)}</td>
            <td className="mono">
              {item.coverage === null
                ? "—"
                : `${(item.coverage * 100).toFixed(0)}%`}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function LiveOptionsPage() {
  const [underlyingId, setUnderlyingId] = useState("");
  const [rate, setRate] = useState("0.065");
  const [settlement, setSettlement] = useState("10:00:00");
  const [jobId, setJobId] = useState<string | null>(null);

  const underlyings = useQuery({
    queryKey: ["option-underlyings"],
    queryFn: () =>
      api.get<{ items: Instrument[] }>("/instruments?asset_class=INDEX&limit=100"),
  });

  const { job, result } = useJob(jobId);
  const payload = result.data?.result as LiveOptionsResult | undefined;

  const openInterest = useQuery({
    queryKey: ["open-interest", underlyingId, payload?.capture.snapshot_id],
    enabled: Boolean(payload?.capture.snapshot_id),
    queryFn: () =>
      api.get<OpenInterestProfile>(
        `/market/open-interest/${underlyingId}?snapshot_id=${payload!.capture.snapshot_id}`,
      ),
  });

  const analyse = useMutation({
    mutationFn: () =>
      api.post<{ job_id: string }>("/live/options/analyse", {
        underlying_id: underlyingId,
        risk_free_rate: rate === "" ? null : Number(rate),
        settlement_time_utc: settlement || null,
      }),
    onSuccess: (data) => setJobId(data.job_id),
  });

  return (
    <>
      <h1>Live options</h1>
      <p className="subtitle">
        Capture the live chain, solve implied volatilities, fit the surface, and
        read its shape. One captured moment feeds all four, so every number
        below traces back to the same quotes.
      </p>

      <ErrorBanner error={analyse.error ?? job.error ?? result.error} />

      <form
        className="card"
        onSubmit={(event) => {
          event.preventDefault();
          analyse.mutate();
        }}
      >
        <div className="field">
          <label htmlFor="underlying">Underlying</label>
          <select
            id="underlying"
            value={underlyingId}
            required
            onChange={(event) => setUnderlyingId(event.target.value)}
          >
            <option value="">Select…</option>
            {(underlyings.data?.items ?? []).map((instrument) => (
              <option key={instrument.id} value={instrument.id}>
                {instrument.symbol} ({instrument.exchange})
              </option>
            ))}
          </select>
        </div>
        <div className="row">
          <div className="field">
            <label htmlFor="rate">Risk-free rate</label>
            <input
              id="rate"
              value={rate}
              onChange={(event) => setRate(event.target.value)}
            />
            <div className="muted" style={{ fontSize: 11 }}>
              Recorded as an assumption, not an observation.
            </div>
          </div>
          <div className="field">
            <label htmlFor="settlement">Settlement time (UTC)</label>
            <input
              id="settlement"
              value={settlement}
              onChange={(event) => setSettlement(event.target.value)}
            />
            <div className="muted" style={{ fontSize: 11 }}>
              Without it, time to expiry is only known to the day.
            </div>
          </div>
        </div>
        <button type="submit" disabled={analyse.isPending || !underlyingId}>
          {analyse.isPending ? "Submitting…" : "Capture and fit"}
        </button>
      </form>

      {job.data && job.data.status !== "COMPLETED" ? (
        <p className="subtitle">
          {job.data.status.toLowerCase()}… capture, implied volatilities, SVI
          calibration.
        </p>
      ) : null}

      {job.data?.error ? (
        <div className="card">
          <h2 style={{ marginTop: 0 }}>The job failed</h2>
          <p className="muted">{JSON.stringify(job.data.error)}</p>
        </div>
      ) : null}

      {payload ? (
        <>
          <CaptureSummary result={payload} />
          <Warnings warnings={payload.warnings ?? []} />

          {payload.analysis ? (
            <div className="card">
              <h2 style={{ marginTop: 0 }}>Implied volatilities</h2>
              <p className="muted">
                {payload.analysis.counts.solved} solved from{" "}
                {payload.analysis.counts.quotes} quotes across{" "}
                {payload.analysis.counts.expiries} expiries.
              </p>
            </div>
          ) : null}

          {payload.greeks ? (
            <div className="card">
              <h2 style={{ marginTop: 0 }}>Greeks</h2>
              <p className="muted">
                {payload.greeks.counts.priced} contracts priced,{" "}
                {payload.greeks.counts.unavailable} without Greeks. Measured
                against each contract&rsquo;s own implied volatility, not the
                fitted surface — these are the Greeks of the option as quoted.
                A contract whose volatility did not solve is listed with a
                reason rather than shown as zero.
              </p>
              <table>
                <thead>
                  <tr>
                    <th>Expiry</th>
                    <th>Forward</th>
                    <th>Priced</th>
                    <th>No Greeks</th>
                  </tr>
                </thead>
                <tbody>
                  {payload.greeks.expiries.map((item) => (
                    <tr key={item.expiry}>
                      <td>{item.expiry}</td>
                      <td className="mono">{num(item.forward, 2)}</td>
                      <td className="mono">{item.counts.priced}</td>
                      <td className="mono">{item.counts.unavailable}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="muted">
                Per unit contract, {payload.greeks.units.vega_per_vol_point}.
                {payload.greeks.dividend_yield_assumed
                  ? " Dividend yield was not supplied and is recorded as assumed."
                  : ""}
              </p>
            </div>
          ) : null}

          {payload.delta_skew ? (
            <div className="card">
              <h2 style={{ marginTop: 0 }}>Skew and smile</h2>
              <p className="muted">
                Risk reversal and butterfly at {payload.delta_skew.levels
                  .map((level) => `${level * 100}Δ`)
                  .join(" and ")}
                , solved on {payload.delta_skew.delta_convention.toLowerCase()}{" "}
                delta. A blank cell is a wing whose strike could not be found —
                which is not the same as a skew of zero.
              </p>
              <DeltaSkewTable slices={payload.delta_skew.slices} />
              {payload.delta_skew.unmeasured.length ? (
                <p className="muted">
                  {payload.delta_skew.unmeasured.length} expiry(ies) produced no
                  measurement:{" "}
                  {payload.delta_skew.unmeasured
                    .map((item) => `${item.expiry} (${item.reason})`)
                    .join("; ")}
                </p>
              ) : null}
            </div>
          ) : null}

          {openInterest.data ? (
            <div className="card">
              <h2 style={{ marginTop: 0 }}>Open interest</h2>
              <p className="muted">
                Counts as the venue reported them, unnormalised — some exchanges
                publish open interest in contracts and some in units of the
                underlying. The ratios are unaffected by that; the totals are.
                These are measurements, not readings of them.
              </p>
              <OpenInterestTable expiries={openInterest.data.expiries} />
            </div>
          ) : null}
        </>
      ) : null}

      <Disclaimer />
    </>
  );
}

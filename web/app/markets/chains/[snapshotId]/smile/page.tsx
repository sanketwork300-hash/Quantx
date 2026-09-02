"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api";
import { SmileChart } from "@/components/SmileChart";
import {
  Disclaimer,
  ErrorBanner,
  Metric,
  ScoreTag,
  SeverityTag,
  Warnings,
} from "@/components/Ui";
import type {
  ChainAnalysis,
  Envelope,
  ForwardEstimate,
  ImpliedVolPoint,
  Job,
  JobResult,
  SmileSlice,
} from "@/lib/types";

function pct(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(digits)}%`;
}

/** Never print more digits than the solver says the price supports. */
function ivWithPrecision(point: ImpliedVolPoint): string {
  if (point.market_iv === null) return "—";
  const uncertainty = point.uncertainty ?? 0;
  const digits = uncertainty > 1e-4 ? 2 : uncertainty > 1e-6 ? 3 : 4;
  return `${(point.market_iv * 100).toFixed(digits)}%`;
}

function ForwardPanel({ slice }: { slice: SmileSlice }) {
  const estimates: ForwardEstimate[] = slice.forward.estimates ?? [];
  return (
    <div>
      <table>
        <thead>
          <tr>
            <th>Method</th>
            <th>Forward</th>
            <th>DF</th>
            <th>Obs</th>
            <th>Residual</th>
            <th>Confidence</th>
          </tr>
        </thead>
        <tbody>
          {estimates.map((estimate) => (
            <tr key={estimate.method}>
              <td>
                {estimate.selected ? <strong>{estimate.method}</strong> : estimate.method}
                {estimate.selected ? " ✓" : ""}
                {estimate.assumptions.length > 0 && (
                  <div className="muted" style={{ fontSize: 11 }}>
                    assumes {estimate.assumptions.join(", ")}
                  </div>
                )}
                {estimate.error && (
                  <div className="muted" style={{ fontSize: 11 }}>
                    {estimate.error}
                  </div>
                )}
              </td>
              <td>{estimate.value === null ? "—" : estimate.value.toFixed(3)}</td>
              <td>
                {estimate.discount_factor === null
                  ? "—"
                  : estimate.discount_factor.toFixed(6)}
              </td>
              <td>{estimate.observations}</td>
              <td>
                {estimate.residual_error === null
                  ? "—"
                  : estimate.residual_error.toExponential(2)}
              </td>
              <td>
                <ScoreTag score={estimate.confidence} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {slice.forward.disagreement !== null && (
        <p className="muted" style={{ fontSize: 11 }}>
          Estimators disagree by {pct(slice.forward.disagreement, 3)}. Disagreement
          usually means an unstated carry or a bad quote — every log-moneyness in
          this slice depends on the chosen forward, so it is shown rather than
          averaged away.
        </p>
      )}
    </div>
  );
}

export default function SmilePage() {
  const params = useParams<{ snapshotId: string }>();
  const [rate, setRate] = useState("0.065");
  const [settlement, setSettlement] = useState("10:00:00");
  const [jobId, setJobId] = useState<string | null>(null);
  const [selected, setSelected] = useState<ImpliedVolPoint | null>(null);

  const smile = useQuery({
    queryKey: ["smile", params.snapshotId],
    queryFn: () =>
      api.get<Envelope<ChainAnalysis>>(
        `/derivatives/chains/${params.snapshotId}/smile?used_for_smile_only=false`,
      ),
    retry: false,
  });

  const analyse = useMutation({
    mutationFn: async () => {
      const accepted = await api.post<{ job_id: string }>(
        `/derivatives/chains/${params.snapshotId}/analyze`,
        {
          risk_free_rate: rate === "" ? 0 : Number(rate),
          dividend_yield: 0,
          settlement_time_utc: settlement === "" ? null : settlement,
        },
      );
      return accepted.job_id;
    },
    onSuccess: (id) => setJobId(id),
  });

  const job = useQuery({
    queryKey: ["analysis-job", jobId],
    queryFn: () => api.get<Job>(`/jobs/${jobId}`),
    enabled: jobId !== null,
    refetchInterval: (query) =>
      query.state.data &&
      ["COMPLETED", "FAILED", "CANCELLED"].includes(query.state.data.status)
        ? false
        : 800,
  });

  const jobResult = useQuery({
    queryKey: ["analysis-job-result", jobId],
    queryFn: async () => {
      const payload = await api.get<JobResult>(`/jobs/${jobId}/result`);
      await smile.refetch();
      return payload;
    },
    enabled: job.data?.status === "COMPLETED",
  });

  const analysis = smile.data?.results;
  const slices = analysis?.slices ?? [];

  return (
    <>
      <h2>Implied volatility</h2>
      <p className="subtitle">
        Market-implied volatility solved from observed prices, against a forward
        estimated from those same prices. No fitted surface exists yet — that is
        Phase 2 — so everything here is an observation or a stated assumption.
      </p>

      <p>
        <Link href={`/markets/chains/${params.snapshotId}`}>← back to the chain</Link>
        {"  ·  "}
        <Link href={`/markets/chains/${params.snapshotId}/surface`}>
          fit a volatility surface →
        </Link>
      </p>

      <ErrorBanner error={analyse.error} />

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Run the analysis</h3>
        <div className="row">
          <div className="field">
            <label htmlFor="rate">Risk-free rate (continuously compounded)</label>
            <input id="rate" value={rate} onChange={(e) => setRate(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="settlement">Settlement time (UTC)</label>
            <input
              id="settlement"
              value={settlement}
              onChange={(e) => setSettlement(e.target.value)}
              placeholder="10:00:00"
            />
          </div>
          <button onClick={() => analyse.mutate()} disabled={analyse.isPending}>
            {analyse.isPending ? "Submitting…" : "Analyse chain"}
          </button>
        </div>
        <p className="muted" style={{ marginBottom: 0 }}>
          Without a settlement time, time to expiry is undefined and no implied
          volatility can be solved — the run will complete and tell you so rather
          than inventing one. The rate is recorded as an assumption; put-call
          parity recovers the discount factor from the quotes regardless.
        </p>
        {job.data && (
          <p style={{ marginBottom: 0 }}>
            <SeverityTag severity={job.data.status === "FAILED" ? "ERROR" : "INFO"} />{" "}
            job {job.data.status}
          </p>
        )}
      </div>

      {jobResult.data?.result && <Warnings warnings={jobResult.data.result.warnings} />}

      {analysis && (
        <>
          <div className="grid">
            <Metric label="Quotes" value={analysis.counts.quotes} />
            <Metric
              label="Solved"
              value={analysis.counts.solved}
              unit={`${analysis.counts.expiries} expiries`}
            />
            <Metric
              label="Underlying"
              value={analysis.underlying_price ?? "—"}
              unit="observed"
            />
            <Metric
              label="Curve"
              value={<span className="mono" style={{ fontSize: 13 }}>{analysis.curve_id ?? "—"}</span>}
              unit="assumption unless stated"
            />
          </div>

          <div className="card">
            <h3 style={{ marginTop: 0 }}>Smile</h3>
            <SmileChart
              slices={slices}
              onSelectPoint={(id) => {
                for (const slice of slices) {
                  const found = slice.points.find((p) => p.instrument_id === id);
                  if (found) setSelected(found);
                }
              }}
            />
          </div>

          {slices.map((slice) => (
            <div className="card" key={slice.expiry}>
              <h3 style={{ marginTop: 0 }}>
                {slice.expiry}
                {slice.settlement_time_assumed && (
                  <span className="tag info" style={{ marginLeft: 8 }}>
                    settlement time assumed
                  </span>
                )}
              </h3>

              {slice.reason ? (
                <div className="banner warn">
                  Not solved: <span className="mono">{slice.reason}</span>
                </div>
              ) : (
                <>
                  <div className="grid">
                    <Metric label="ATM volatility" value={pct(slice.atm_volatility)} />
                    <Metric
                      label="Skew"
                      value={slice.skew === null ? "—" : slice.skew.toFixed(4)}
                      unit="dσ/dk near the money"
                    />
                    <Metric
                      label="Curvature"
                      value={slice.curvature === null ? "—" : slice.curvature.toFixed(3)}
                      unit="d²σ/dk²"
                    />
                    <Metric
                      label="Used for smile"
                      value={`${slice.counts.used_for_smile} / ${slice.counts.solved}`}
                      unit="one side per strike"
                    />
                  </div>

                  <h3>Forward</h3>
                  <ForwardPanel slice={slice} />

                  {Object.keys(slice.solve_failures).length > 0 && (
                    <>
                      <h3>Quotes with no implied volatility</h3>
                      <ul className="reasons">
                        {Object.entries(slice.solve_failures).map(([code, count]) => (
                          <li key={code}>
                            <span className="mono">{code}</span> — {count}
                          </li>
                        ))}
                      </ul>
                    </>
                  )}

                  <h3>Points</h3>
                  <div className="table-wrap" style={{ maxHeight: 340 }}>
                    <table>
                      <thead>
                        <tr>
                          <th>Strike</th>
                          <th>Type</th>
                          <th>k</th>
                          <th>Price</th>
                          <th>Market IV</th>
                          <th>IV bid</th>
                          <th>IV ask</th>
                          <th>Envelope</th>
                          <th>w</th>
                          <th>Solver</th>
                          <th>Used</th>
                        </tr>
                      </thead>
                      <tbody>
                        {slice.points.map((point) => (
                          <tr
                            key={point.instrument_id}
                            onClick={() => setSelected(point)}
                            style={{ cursor: "pointer" }}
                            className={point.used_for_smile ? "" : "excluded"}
                          >
                            <td>{point.strike}</td>
                            <td>{point.option_type === "CALL" ? "C" : "P"}</td>
                            <td>{point.log_moneyness?.toFixed(4) ?? "—"}</td>
                            <td>{point.price_used?.toFixed(2) ?? "—"}</td>
                            <td>{ivWithPrecision(point)}</td>
                            <td>{pct(point.market_iv_bid)}</td>
                            <td>{pct(point.market_iv_ask)}</td>
                            <td>{pct(point.iv_envelope_width, 2)}</td>
                            <td>{point.total_variance?.toFixed(5) ?? "—"}</td>
                            <td className="mono" style={{ fontSize: 11 }}>
                              {point.error ?? point.solver}
                            </td>
                            <td className="mono" style={{ fontSize: 11 }}>
                              {point.used_for_smile ? "✓" : point.smile_exclusion ?? ""}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </div>
          ))}

          {selected && (
            <div className="card">
              <h3 style={{ marginTop: 0 }}>
                How well determined is this implied volatility?
              </h3>
              <p className="mono">
                {selected.expiry} {selected.strike} {selected.option_type}
              </p>
              <table>
                <tbody>
                  <tr>
                    <td>Market IV</td>
                    <td>{ivWithPrecision(selected)}</td>
                  </tr>
                  <tr>
                    <td>
                      Bid/ask envelope
                      <div className="muted" style={{ fontSize: 11 }}>
                        how much of any deviation is just the spread
                      </div>
                    </td>
                    <td>
                      {pct(selected.market_iv_bid)} – {pct(selected.market_iv_ask)}
                    </td>
                  </tr>
                  <tr>
                    <td>
                      Vega
                      <div className="muted" style={{ fontSize: 11 }}>
                        near zero means the price carries little volatility information
                      </div>
                    </td>
                    <td>{selected.vega?.toExponential(3) ?? "—"}</td>
                  </tr>
                  <tr>
                    <td>
                      Numerical uncertainty
                      <div className="muted" style={{ fontSize: 11 }}>
                        volatility moved by one price tick of rounding
                      </div>
                    </td>
                    <td>
                      {selected.uncertainty === null
                        ? "—"
                        : selected.uncertainty.toExponential(2)}
                    </td>
                  </tr>
                  <tr>
                    <td>Solver</td>
                    <td className="mono">
                      {selected.solver} · {selected.iterations} iterations ·{" "}
                      {selected.converged ? "converged" : "did not converge"}
                    </td>
                  </tr>
                  <tr>
                    <td>Price used</td>
                    <td>
                      {selected.price_used?.toFixed(4) ?? "—"}{" "}
                      <span className="mono muted">({selected.price_source})</span>
                    </td>
                  </tr>
                  {selected.error && (
                    <tr>
                      <td>No result because</td>
                      <td className="mono">{selected.error}</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {!analysis && !smile.isLoading && (
        <div className="banner note">
          No analysis for this chain yet. Run one above.
        </div>
      )}

      <Disclaimer />
    </>
  );
}

"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api";
import {
  Disclaimer,
  ErrorBanner,
  Metric,
  ScoreTag,
  SeverityTag,
  Warnings,
} from "@/components/Ui";
import type {
  AnomalyScan,
  ChainAnalysis,
  Envelope,
  Job,
  JobResult,
  SurfaceAnomaly,
  SurfaceHistory,
} from "@/lib/types";

function volPoints(value: number | null | undefined, digits = 3): string {
  return value === null || value === undefined ? "—" : (value * 100).toFixed(digits);
}

function EffectTag({ effect }: { effect: string }) {
  const tone = effect === "SUPPORTS" ? "good" : effect === "REDUCES" ? "warn" : "info";
  return <span className={`tag ${tone}`}>{effect.toLowerCase()}</span>;
}

/** §80: every advanced number must be explainable from actual measurements. */
function ExplanationPanel({ anomaly }: { anomaly: SurfaceAnomaly }) {
  return (
    <div className="card" style={{ width: 420, flex: "0 0 420px" }}>
      <h3 style={{ marginTop: 0 }}>Why was this flagged?</h3>
      <p className="mono">
        {anomaly.expiry} {anomaly.strike} {anomaly.option_type}
      </p>

      <table>
        <tbody>
          <tr>
            <td>Observed implied volatility</td>
            <td>{volPoints(anomaly.market_iv, 3)}%</td>
          </tr>
          <tr>
            <td>
              Reference from the surface
              <div className="muted" style={{ fontSize: 11 }}>
                a model output, not a fair value
              </div>
            </td>
            <td>{volPoints(anomaly.reference_iv, 3)}%</td>
          </tr>
          <tr>
            <td>
              <strong>Difference</strong>
            </td>
            <td>
              <strong>{anomaly.iv_difference_vol_points.toFixed(3)} vol pts</strong>
            </td>
          </tr>
          <tr>
            <td>
              Quoted range in volatility
              <div className="muted" style={{ fontSize: 11 }}>
                how much of it the market&apos;s own width explains
              </div>
            </td>
            <td>
              {volPoints(anomaly.market_iv_bid, 3)}% – {volPoints(anomaly.market_iv_ask, 3)}%
              <div className="muted" style={{ fontSize: 11 }}>
                reference sits {anomaly.envelope_position.replace("_", " ").toLowerCase()}
              </div>
            </td>
          </tr>
          <tr>
            <td>
              Explained scale
              <div className="muted" style={{ fontSize: 11 }}>
                bid/ask width, fit error and measurement resolution combined
              </div>
            </td>
            <td>{volPoints(anomaly.explained_scale, 4)} vol pts</td>
          </tr>
          <tr>
            <td>
              <strong>Standardised deviation</strong>
            </td>
            <td>
              <strong>{anomaly.z_score.toFixed(2)}×</strong>
            </td>
          </tr>
          <tr>
            <td>
              Against its own history
              <div className="muted" style={{ fontSize: 11 }}>
                {anomaly.historical_observations} prior observation
                {anomaly.historical_observations === 1 ? "" : "s"}
              </div>
            </td>
            <td>
              {anomaly.historical_z_score === null
                ? "—"
                : `${anomaly.historical_z_score.toFixed(2)}σ`}
            </td>
          </tr>
          <tr>
            <td>
              <strong>Confidence</strong>
            </td>
            <td>
              <ScoreTag score={anomaly.confidence} />
            </td>
          </tr>
        </tbody>
      </table>

      <h3>What went into that confidence</h3>
      <ul className="reasons">
        {anomaly.explanation.map((entry, index) => (
          <li key={index} style={{ marginBottom: 6 }}>
            <EffectTag effect={entry.effect} /> <strong>{entry.factor}</strong>
            <div className="muted">{entry.detail}</div>
          </li>
        ))}
      </ul>

      <p className="muted" style={{ fontSize: 11 }}>
        This is a measured difference between an observed quote and a fitted
        model, with the reasons it may or may not be meaningful. It is not a
        view on what the contract is worth or on what to do about it.
      </p>
    </div>
  );
}

function HistoryPanel({ history }: { history: SurfaceHistory }) {
  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>Surface history</h3>
      <p className="muted" style={{ marginTop: 0 }}>
        Where today&apos;s shape sits against this underlying&apos;s own past, at
        fixed tenors so surfaces stay comparable as expiries roll.
      </p>
      <div className="table-wrap" style={{ maxHeight: 320 }}>
        <table>
          <thead>
            <tr>
              <th>Tenor</th>
              <th>Characteristic</th>
              <th>Current</th>
              <th>Percentile</th>
              <th>z</th>
              <th>Observations</th>
            </tr>
          </thead>
          <tbody>
            {history.tenors.flatMap((tenor) =>
              tenor.percentiles.map((percentile) => (
                <tr key={`${tenor.tenor_days}-${percentile.name}`}>
                  <td>{tenor.tenor_days}d</td>
                  <td>{percentile.name.replace(/_/g, " ")}</td>
                  <td>
                    {percentile.current === null
                      ? "—"
                      : percentile.name.includes("volatility")
                        ? `${(percentile.current * 100).toFixed(3)}%`
                        : percentile.current.toFixed(4)}
                  </td>
                  <td>
                    {percentile.percentile === null
                      ? "—"
                      : `${(percentile.percentile * 100).toFixed(0)}%`}
                  </td>
                  <td>{percentile.z_score === null ? "—" : percentile.z_score.toFixed(2)}</td>
                  <td>
                    {tenor.observations}
                    {!percentile.is_reliable && (
                      <span className="tag warn" style={{ marginLeft: 6 }}>
                        thin
                      </span>
                    )}
                  </td>
                </tr>
              )),
            )}
          </tbody>
        </table>
      </div>
      <p className="muted" style={{ fontSize: 11 }}>
        A percentile from a handful of surfaces is not a distribution. Rows
        marked <em>thin</em> have fewer than{" "}
        {history.tenors[0]?.minimum_reliable_observations ?? 20} observations and
        are shown for completeness, not for inference.
      </p>
    </div>
  );
}

export default function ScannerPage() {
  const params = useParams<{ snapshotId: string }>();
  const [jobId, setJobId] = useState<string | null>(null);
  const [selected, setSelected] = useState<SurfaceAnomaly | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [minZ, setMinZ] = useState("2");

  const smile = useQuery({
    queryKey: ["smile-for-scan", params.snapshotId],
    queryFn: () =>
      api.get<Envelope<ChainAnalysis>>(
        `/derivatives/chains/${params.snapshotId}/smile?used_for_smile_only=true`,
      ),
    retry: false,
  });
  const underlyingId = smile.data?.results?.underlying_id;
  const analysisId = smile.data?.results?.analysis_id;

  const surfaces = useQuery({
    queryKey: ["surfaces"],
    queryFn: () =>
      api.get<{ surface_row_id: string; analysis_id: string }[]>("/derivatives/surfaces"),
    retry: false,
  });
  const surfaceRowId = surfaces.data?.find((s) => s.analysis_id === analysisId)
    ?.surface_row_id;

  const scan = useQuery({
    queryKey: ["anomalies", underlyingId, showAll],
    queryFn: () =>
      api.get<Envelope<AnomalyScan>>(
        `/derivatives/anomalies/${underlyingId}?flagged_only=${!showAll}&limit=500`,
      ),
    enabled: Boolean(underlyingId),
    retry: false,
  });

  const history = useQuery({
    queryKey: ["surface-history", underlyingId],
    queryFn: () =>
      api.get<Envelope<SurfaceHistory>>(
        `/derivatives/history/${underlyingId}?include_series=false`,
      ),
    enabled: Boolean(underlyingId),
    retry: false,
  });

  const run = useMutation({
    mutationFn: async () => {
      if (!surfaceRowId) throw new Error("Fit a volatility surface first.");
      const accepted = await api.post<{ job_id: string }>(
        `/derivatives/surfaces/${surfaceRowId}/anomalies`,
        { min_z_score: Number(minZ) || 2, require_outside_envelope: true },
      );
      return accepted.job_id;
    },
    onSuccess: (id) => setJobId(id),
  });

  const job = useQuery({
    queryKey: ["scan-job", jobId],
    queryFn: () => api.get<Job>(`/jobs/${jobId}`),
    enabled: jobId !== null,
    refetchInterval: (query) =>
      query.state.data &&
      ["COMPLETED", "FAILED", "CANCELLED"].includes(query.state.data.status)
        ? false
        : 800,
  });

  const jobResult = useQuery({
    queryKey: ["scan-job-result", jobId],
    queryFn: async () => {
      const payload = await api.get<JobResult>(`/jobs/${jobId}/result`);
      await scan.refetch();
      await history.refetch();
      return payload;
    },
    enabled: job.data?.status === "COMPLETED",
  });

  const results = scan.data?.results;

  return (
    <>
      <h2>Surface scanner</h2>
      <p className="subtitle">
        Observed implied volatilities compared against the fitted reference
        surface. What this produces is a measured difference, the scale of
        everything that could explain it, and a confidence grounded in named
        measurements — not a view on any contract.
      </p>

      <p>
        <Link href={`/markets/chains/${params.snapshotId}/surface`}>← volatility surface</Link>
      </p>

      <ErrorBanner error={run.error} />

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Run a scan</h3>
        <div className="row">
          <div className="field" style={{ marginBottom: 0 }}>
            <label htmlFor="minz">Minimum standardised deviation</label>
            <input id="minz" value={minZ} onChange={(e) => setMinZ(e.target.value)} />
          </div>
          <button onClick={() => run.mutate()} disabled={!surfaceRowId || run.isPending}>
            {run.isPending ? "Submitting…" : "Scan"}
          </button>
          {job.data && (
            <span>
              <SeverityTag severity={job.data.status === "FAILED" ? "ERROR" : "INFO"} /> job{" "}
              {job.data.status}
            </span>
          )}
        </div>
        <p className="muted" style={{ marginBottom: 0 }}>
          A quote is flagged when the difference exceeds this many times the
          combined size of the bid/ask width, the slice&apos;s calibration error
          and the numerical resolution of the inversion — and when the
          market&apos;s own quoted range does not already account for it. A fixed
          threshold on the volatility difference alone would flag every illiquid
          wing quote and nothing else.
        </p>
      </div>

      {jobResult.data?.result && <Warnings warnings={jobResult.data.result.warnings} />}

      {results && (
        <>
          <div className="grid">
            <Metric label="Quotes examined" value={results.counts.examined} />
            <Metric label="Scored" value={results.counts.scored} />
            <Metric
              label="Flagged"
              value={results.counts.flagged}
              unit={`z ≥ ${String(results.policy.min_z_score ?? "")}`}
            />
            <Metric
              label="As of"
              value={new Date(results.as_of_timestamp).toISOString().slice(0, 19)}
              unit="UTC"
            />
          </div>

          <div className="card">
            <div className="row">
              <label style={{ marginBottom: 0 }}>
                <input
                  type="checkbox"
                  checked={showAll}
                  onChange={(event) => setShowAll(event.target.checked)}
                />{" "}
                Show every scored quote, not only the flagged ones
              </label>
              <span className="muted">{results.anomalies.length} rows</span>
            </div>
          </div>

          <div style={{ display: "flex", gap: 16, alignItems: "flex-start" }}>
            <div className="table-wrap" style={{ flex: 1, maxHeight: 560 }}>
              <table>
                <thead>
                  <tr>
                    <th>Expiry</th>
                    <th>Strike</th>
                    <th>Type</th>
                    <th>Market IV</th>
                    <th>Reference IV</th>
                    <th>Difference</th>
                    <th>z</th>
                    <th>Envelope</th>
                    <th>Liquidity</th>
                    <th>Confidence</th>
                  </tr>
                </thead>
                <tbody>
                  {results.anomalies.map((anomaly) => (
                    <tr
                      key={anomaly.instrument_id}
                      onClick={() => setSelected(anomaly)}
                      style={{ cursor: "pointer" }}
                      className={anomaly.flagged ? "" : "excluded"}
                    >
                      <td>{anomaly.expiry}</td>
                      <td>{anomaly.strike}</td>
                      <td>{anomaly.option_type === "CALL" ? "C" : "P"}</td>
                      <td>{volPoints(anomaly.market_iv, 3)}%</td>
                      <td>{volPoints(anomaly.reference_iv, 3)}%</td>
                      <td>{anomaly.iv_difference_vol_points.toFixed(3)}</td>
                      <td>{anomaly.z_score.toFixed(2)}</td>
                      <td className="mono" style={{ fontSize: 11 }}>
                        {anomaly.envelope_position.replace("_", " ").toLowerCase()}
                      </td>
                      <td>
                        <ScoreTag score={anomaly.liquidity_score} />
                      </td>
                      <td>
                        <ScoreTag score={anomaly.confidence} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {selected ? (
              <ExplanationPanel anomaly={selected} />
            ) : (
              <div className="card" style={{ width: 420, flex: "0 0 420px" }}>
                <h3 style={{ marginTop: 0 }}>Select a row</h3>
                <p className="muted">
                  Every flagged quote can account for itself: what deviated, by
                  how much, relative to which reference, against what scale, and
                  what raises or lowers confidence in the measurement.
                </p>
              </div>
            )}
          </div>
        </>
      )}

      {history.data?.results && <HistoryPanel history={history.data.results} />}

      {!results && !scan.isLoading && (
        <div className="banner note">
          No scan for this underlying yet.{" "}
          {surfaceRowId ? "Run one above." : "Fit a volatility surface first."}
        </div>
      )}

      <Disclaimer />
    </>
  );
}

"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { use, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { Disclaimer, ErrorBanner, Metric, SeverityTag, Warnings } from "@/components/Ui";
import type {
  BookAnalyticsReport,
  DatasetDetail,
  Envelope,
  IntensityComparison,
  Job,
  JobResult,
  QueueOutlookOut,
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
  if (!Number.isFinite(value)) return "never, at this rate";
  return value.toLocaleString(undefined, { maximumFractionDigits: digits });
}

export default function DatasetPage({
  params,
}: {
  params: Promise<{ datasetId: string }>;
}) {
  const { datasetId } = use(params);
  const [analysisJob, setAnalysisJob] = useState<string | null>(null);
  const [intensityJob, setIntensityJob] = useState<string | null>(null);
  const [scope, setScope] = useState<string>("");
  const [side, setSide] = useState<string>("BID");
  const [horizon, setHorizon] = useState("300");
  const [queue, setQueue] = useState<Envelope<QueueOutlookOut> | null>(null);

  const dataset = useQuery({
    queryKey: ["microstructure-dataset", datasetId],
    queryFn: () =>
      api.get<Envelope<DatasetDetail>>(`/microstructure/datasets/${datasetId}`),
  });
  const rejections = useQuery({
    queryKey: ["microstructure-rejections", datasetId],
    queryFn: () =>
      api.get<{
        snapshot_rejections: { row_number: number; reason: string; message: string }[];
        event_rejections: { row_number: number; reason: string; message: string }[];
        counts: Record<string, number>;
      }>(`/microstructure/datasets/${datasetId}/rejections`),
  });

  const detail = dataset.data?.results ?? null;
  const verdicts = detail?.availability.capabilities ?? [];
  const allows = (name: string) =>
    verdicts.some((item) => item.capability === name && item.available);

  const analyse = useMutation({
    mutationFn: async () => {
      const accepted = await api.post<{ job_id: string }>(
        `/microstructure/datasets/${datasetId}/analyze`,
        { levels: 5, weighted_decay: 0.5, trade_sizes: [500] },
      );
      return accepted.job_id;
    },
    onSuccess: setAnalysisJob,
  });
  const fit = useMutation({
    mutationFn: async () => {
      const accepted = await api.post<{ job_id: string }>(
        `/microstructure/datasets/${datasetId}/intensity`,
        { event_types: scope ? [scope] : [], train_fraction: 0.7 },
      );
      return accepted.job_id;
    },
    onSuccess: setIntensityJob,
  });
  const estimate = useMutation({
    mutationFn: () =>
      api.post<Envelope<QueueOutlookOut>>(
        `/microstructure/datasets/${datasetId}/queue`,
        { side, horizon_seconds: Number(horizon) || 60 },
      ),
    onSuccess: setQueue,
  });

  const analysis = useJob(analysisJob);
  const intensity = useJob(intensityJob);
  const report = (
    analysis.result.data?.result as unknown as Envelope<BookAnalyticsReport> | undefined
  )?.results;
  const comparison = (
    intensity.result.data?.result as unknown as Envelope<IntensityComparison> | undefined
  )?.results;

  return (
    <>
      <h2>{detail?.name ?? "Dataset"}</h2>
      <p className="subtitle">
        {detail?.kind} · {detail?.rows.snapshots.kept ?? 0} snapshots ·{" "}
        {detail?.rows.events.kept ?? 0} events ·{" "}
        {Math.round((detail?.window.span_seconds ?? 0) / 60)} minutes
      </p>

      <ErrorBanner
        error={
          dataset.error ??
          analyse.error ??
          fit.error ??
          (estimate.error as ApiError | null)
        }
      />

      <div className="card">
        <h3 style={{ marginTop: 0 }}>What this dataset can answer</h3>
        <p className="muted" style={{ marginTop: 0 }}>
          Decided once, when the data was imported, and consulted before anything
          runs. A refusal names what was missing and shows the numbers it was
          decided on, so it can be argued with rather than only obeyed.
        </p>
        <ul className="reasons">
          {verdicts.map((verdict) => (
            <li key={verdict.capability}>
              <span className={`tag ${verdict.available ? "good" : "bad"}`}>
                {verdict.available ? "granted" : "refused"}
              </span>{" "}
              <span className="mono">{verdict.capability}</span>
              {verdict.reason && (
                <>
                  {" "}
                  <span className="mono muted">{verdict.reason}</span>
                </>
              )}
              <div className="muted">{verdict.message}</div>
            </li>
          ))}
        </ul>
      </div>

      {/* ------------------------------------------------------- book measures */}
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Book measures</h3>
        <p className="muted" style={{ marginTop: 0 }}>
          Percentiles rather than a mean and a standard deviation: a session of
          books is not normal, and a handful of instants around the auction own
          the variance. Every measure says how many snapshots it was computed
          over and how many had no such measurement.
        </p>
        <button onClick={() => analyse.mutate()} disabled={!allows("TOP_OF_BOOK")}>
          {analyse.isPending ? "Submitting…" : "Measure every snapshot"}
        </button>
        {!allows("TOP_OF_BOOK") && (
          <span className="muted"> — refused on this dataset, see above.</span>
        )}
        {analysis.job.data && (
          <p>
            <SeverityTag
              severity={analysis.job.data.status === "FAILED" ? "ERROR" : "INFO"}
            />{" "}
            {analysis.job.data.status}
          </p>
        )}

        {report && (
          <>
            <div className="table-wrap" style={{ marginTop: 12 }}>
              <table>
                <thead>
                  <tr>
                    <th>Measure</th>
                    <th>p05</th>
                    <th>p50</th>
                    <th>p95</th>
                    <th>Observations</th>
                    <th>No measurement</th>
                  </tr>
                </thead>
                <tbody>
                  {report.measures.map((measure) => (
                    <tr key={measure.measure}>
                      <td className="mono">{measure.measure}</td>
                      <td>{num(measure.percentiles.p05)}</td>
                      <td>{num(measure.percentiles.p50)}</td>
                      <td>{num(measure.percentiles.p95)}</td>
                      <td>{measure.observations}</td>
                      <td className="muted">
                        {measure.missing === 0
                          ? "—"
                          : `${measure.missing} (${Object.keys(
                              measure.missing_reasons,
                            ).join(", ")})`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <h3>Cost of taking the displayed book</h3>
            <div className="table-wrap" style={{ maxHeight: 220 }}>
              <table>
                <thead>
                  <tr>
                    <th>Size</th>
                    <th>Side</th>
                    <th>Median slippage (bps)</th>
                    <th>p95</th>
                    <th>Levels consumed</th>
                    <th>Snapshots that could not absorb it</th>
                  </tr>
                </thead>
                <tbody>
                  {report.trade_costs.map((cost) => (
                    <tr key={`${cost.quantity}-${cost.side}`}>
                      <td>{cost.quantity.toLocaleString()}</td>
                      <td>{cost.side}</td>
                      <td>{num(cost.median_slippage_bps, 2)}</td>
                      <td>{num(cost.p95_slippage_bps, 2)}</td>
                      <td>{num(cost.median_levels_consumed, 1)}</td>
                      <td>
                        {cost.snapshots_that_could_not > 0 ? (
                          <span className="tag warn">
                            {cost.snapshots_that_could_not}
                          </span>
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="muted">{report.trade_costs[0]?.note}</p>
          </>
        )}
      </div>

      {/* ---------------------------------------------------------- intensity */}
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Arrival intensity</h3>
        <p className="muted" style={{ marginTop: 0 }}>
          A constant rate, and a self-exciting model that has to beat it on
          events it was not fitted on. Both are shown whatever the verdict,
          because &ldquo;it was tried and did not earn its parameters here&rdquo;
          is the evidence that the comparison ran.
        </p>
        <div className="row">
          <div className="field">
            <label htmlFor="scope">Events</label>
            <select
              id="scope"
              value={scope}
              onChange={(event) => setScope(event.target.value)}
            >
              <option value="">All messages</option>
              <option value="ADD">Adds</option>
              <option value="CANCEL">Cancellations</option>
              <option value="TRADE">Trades</option>
            </select>
          </div>
          <div className="field">
            <button onClick={() => fit.mutate()} disabled={!allows("EVENT_INTENSITY")}>
              {fit.isPending ? "Submitting…" : "Fit and compare"}
            </button>
          </div>
        </div>
        {intensity.job.data && (
          <p>
            <SeverityTag
              severity={intensity.job.data.status === "FAILED" ? "ERROR" : "INFO"}
            />{" "}
            {intensity.job.data.status}
          </p>
        )}

        {comparison && (
          <>
            <div
              className={`banner ${comparison.hawkes_is_adopted ? "note" : "warn"}`}
            >
              {comparison.reason}
            </div>

            <div className="row">
              <Metric
                label="Reported model"
                value={comparison.adopted_model}
                unit={`${num(comparison.adopted_rate_per_second, 3)} events/s`}
              />
              <Metric
                label="Held-out test"
                value={num(comparison.predictive_test.statistic, 2)}
                unit={`vs ${comparison.predictive_test.critical_value} threshold`}
              />
              <Metric
                label="Held-out events"
                value={comparison.held_out_events}
                unit="scored, not fitted on"
              />
            </div>

            <div className="table-wrap" style={{ maxHeight: 320 }}>
              <table>
                <thead>
                  <tr>
                    <th>Model</th>
                    <th>Parameters</th>
                    <th>Train log-likelihood</th>
                    <th>Held-out</th>
                    <th>KS vs Exp(1)</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td className="mono">POISSON</td>
                    <td className="mono">
                      rate {num(comparison.poisson.train.parameters.rate as number, 4)}
                    </td>
                    <td>{num(comparison.poisson.train.log_likelihood, 1)}</td>
                    <td>{num(comparison.poisson.held_out_log_likelihood, 1)}</td>
                    <td>{num(comparison.poisson.train.ks_statistic, 3)}</td>
                  </tr>
                  <tr
                    style={{
                      opacity: comparison.hawkes_is_adopted ? 1 : 0.55,
                    }}
                  >
                    <td className="mono">
                      HAWKES{" "}
                      {!comparison.hawkes_is_adopted && (
                        <span className="tag warn">not adopted</span>
                      )}
                    </td>
                    <td className="mono">
                      n ={" "}
                      {num(
                        comparison.hawkes.train.parameters.branching_ratio as number,
                        3,
                      )}
                      , half-life{" "}
                      {num(
                        comparison.hawkes.train.parameters
                          .excitation_half_life_seconds as number,
                        3,
                      )}
                      s
                    </td>
                    <td>{num(comparison.hawkes.train.log_likelihood, 1)}</td>
                    <td>{num(comparison.hawkes.held_out_log_likelihood, 1)}</td>
                    <td>{num(comparison.hawkes.train.ks_statistic, 3)}</td>
                  </tr>
                </tbody>
              </table>
            </div>

            <p className="muted">{comparison.method}</p>
            <p className="muted" style={{ marginBottom: 0 }}>
              {comparison.interpretation}
            </p>
          </>
        )}
      </div>

      {/* -------------------------------------------------------------- queue */}
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Queue outlook</h3>
        <p className="muted" style={{ marginTop: 0 }}>
          The answer is a range, and the range is the answer. Its two ends differ
          only in whether cancellations at the level are assumed to remove size
          ahead of the order or behind it, and a public feed does not say which.
        </p>
        <div className="row">
          <div className="field">
            <label htmlFor="side">Side</label>
            <select
              id="side"
              value={side}
              onChange={(event) => setSide(event.target.value)}
            >
              <option value="BID">BID</option>
              <option value="ASK">ASK</option>
            </select>
          </div>
          <div className="field">
            <label htmlFor="horizon">Horizon (seconds)</label>
            <input
              id="horizon"
              value={horizon}
              onChange={(event) => setHorizon(event.target.value)}
            />
          </div>
          <div className="field">
            <button
              onClick={() => estimate.mutate()}
              disabled={!allows("QUEUE_POSITION")}
            >
              {estimate.isPending ? "Estimating…" : "Estimate"}
            </button>
          </div>
        </div>

        {queue?.warnings?.length ? <Warnings warnings={queue.warnings} /> : null}

        {queue?.results && (
          <>
            <div className="row">
              <Metric
                label="Fill probability"
                value={`${(queue.results.estimated_fill_probability_range[0] * 100).toFixed(1)}% – ${(
                  queue.results.estimated_fill_probability_range[1] * 100
                ).toFixed(1)}%`}
                unit={`within ${queue.results.horizon_seconds}s`}
              />
              <Metric
                label="Wait"
                value={`${num(queue.results.estimated_wait_seconds_range[0], 0)} – ${num(
                  queue.results.estimated_wait_seconds_range[1],
                  0,
                )} s`}
                unit="fastest to slowest assumption"
              />
              <Metric
                label="Ahead of the order"
                value={num(queue.results.estimated_queue_position, 0)}
                unit={`of ${num(queue.results.level_quantity, 0)} displayed`}
              />
              <Metric
                label="Confidence"
                value={queue.results.confidence.toFixed(2)}
                unit="evidence x agreement x coverage"
              />
            </div>

            <div className="table-wrap" style={{ maxHeight: 240 }}>
              <table>
                <thead>
                  <tr>
                    <th>Assumption</th>
                    <th>Departure rate</th>
                    <th>Events needed</th>
                    <th>Wait</th>
                    <th>Fill probability</th>
                  </tr>
                </thead>
                <tbody>
                  {[queue.results.optimistic, queue.results.pessimistic].map((end) => (
                    <tr key={end.priority_assumption}>
                      <td className="mono">{end.priority_assumption}</td>
                      <td>{num(end.departure_rate_per_second, 2)} /s</td>
                      <td>{end.events_required}</td>
                      <td>{num(end.estimated_wait_seconds, 0)} s</td>
                      <td>{(end.estimated_fill_probability * 100).toFixed(1)}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <h3>What this assumes</h3>
            <ul className="reasons">
              {queue.results.assumptions.map((assumption) => (
                <li key={assumption}>{assumption}</li>
              ))}
            </ul>
            <p className="muted" style={{ marginBottom: 0 }}>
              {queue.results.interpretation}
            </p>
          </>
        )}
      </div>

      {/* --------------------------------------------------------- rejections */}
      {rejections.data &&
        (rejections.data.snapshot_rejections.length > 0 ||
          rejections.data.event_rejections.length > 0) && (
          <div className="card">
            <h3 style={{ marginTop: 0 }}>Rows that did not make it</h3>
            <p className="muted" style={{ marginTop: 0 }}>
              The complete list, not a sample. Every one carries the row number
              it had in your own file.
            </p>
            <div className="table-wrap" style={{ maxHeight: 320 }}>
              <table>
                <thead>
                  <tr>
                    <th>Half</th>
                    <th>Row</th>
                    <th>Reason</th>
                    <th>Why</th>
                  </tr>
                </thead>
                <tbody>
                  {[
                    ...rejections.data.snapshot_rejections.map((row) => ({
                      half: "snapshot",
                      ...row,
                    })),
                    ...rejections.data.event_rejections.map((row) => ({
                      half: "event",
                      ...row,
                    })),
                  ].map((row) => (
                    <tr key={`${row.half}-${row.row_number}-${row.reason}`}>
                      <td>{row.half}</td>
                      <td>{row.row_number}</td>
                      <td className="mono">{row.reason}</td>
                      <td className="muted">{row.message}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

      <div className="card">
        <p style={{ margin: 0 }}>
          <Link href="/microstructure">Back to datasets →</Link>
        </p>
      </div>

      <Disclaimer />
    </>
  );
}

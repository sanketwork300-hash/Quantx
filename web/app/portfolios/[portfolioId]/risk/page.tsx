"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api";
import { Disclaimer, ErrorBanner, Metric, SeverityTag, Warnings } from "@/components/Ui";
import type {
  Envelope,
  Job,
  JobResult,
  Portfolio,
  Scenario,
  StressResult,
  VaRResult,
} from "@/lib/types";

const METHODS = [
  {
    id: "HISTORICAL",
    label: "Historical",
    note: "Reprices the book under every move the platform has recorded. No distribution is assumed.",
  },
  {
    id: "MONTE_CARLO",
    label: "Monte Carlo",
    note: "Simulates factor moves from the estimated covariance, then reprices. Seeded, so it repeats exactly.",
  },
  {
    id: "PARAMETRIC",
    label: "Parametric",
    note: "Covariance times a normal quantile on a delta-and-vega linearisation. Not valid alone for an option book.",
  },
];

function money(value: number | null | undefined) {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString(undefined, { maximumFractionDigits: 0 });
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

export default function RiskPage() {
  const { portfolioId } = useParams<{ portfolioId: string }>();
  const [rate, setRate] = useState("0.065");
  const [settlement, setSettlement] = useState("10:00");
  const [method, setMethod] = useState("HISTORICAL");
  const [paths, setPaths] = useState("10000");
  const [seed, setSeed] = useState("20260924");
  const [horizon, setHorizon] = useState("1");
  const [scenario, setScenario] = useState("Underlying -10%");
  const [decay, setDecay] = useState("0");
  const [varJob, setVarJob] = useState<string | null>(null);
  const [stressJob, setStressJob] = useState<string | null>(null);

  const portfolio = useQuery({
    queryKey: ["portfolio", portfolioId],
    queryFn: () => api.get<Portfolio>(`/portfolios/${portfolioId}`),
  });

  const scenarios = useQuery({
    queryKey: ["scenarios"],
    queryFn: () => api.get<Scenario[]>("/scenarios"),
  });

  const common = () => ({
    risk_free_rate: rate === "" ? 0 : Number(rate),
    dividend_yield: 0,
    settlement_time_utc: settlement === "" ? null : `${settlement}:00`,
  });

  const runVar = useMutation({
    mutationFn: async () => {
      const accepted = await api.post<{ job_id: string }>(
        `/portfolios/${portfolioId}/var`,
        {
          ...common(),
          method,
          horizon_days: Number(horizon) || 1,
          confidences: [0.95, 0.99],
          paths: Number(paths) || 10000,
          seed: Number(seed) || 0,
        },
      );
      return accepted.job_id;
    },
    onSuccess: (id) => setVarJob(id),
  });

  const runStress = useMutation({
    mutationFn: async () => {
      const accepted = await api.post<{ job_id: string }>(
        `/portfolios/${portfolioId}/stress`,
        { ...common(), scenario, time_decay_days: Number(decay) || 0 },
      );
      return accepted.job_id;
    },
    onSuccess: (id) => setStressJob(id),
  });

  const varStatus = useJob(varJob);
  const stressStatus = useJob(stressJob);

  const varResult = useQuery({
    queryKey: ["job-result", varJob],
    queryFn: () => api.get<JobResult>(`/jobs/${varJob}/result`),
    enabled: varStatus.data?.status === "COMPLETED",
  });
  const stressResult = useQuery({
    queryKey: ["job-result", stressJob],
    queryFn: () => api.get<JobResult>(`/jobs/${stressJob}/result`),
    enabled: stressStatus.data?.status === "COMPLETED",
  });

  const varEnvelope = varResult.data?.result as Envelope<VaRResult> | undefined;
  const stressEnvelope = stressResult.data?.result as
    | Envelope<StressResult>
    | undefined;
  const risk = varEnvelope?.results;
  const stress = stressEnvelope?.results;
  const selected = scenarios.data?.find((item) => item.name === scenario);

  return (
    <>
      <h2>Risk — {portfolio.data?.name ?? "portfolio"}</h2>
      <p className="subtitle">
        Value at Risk and scenario stress, both computed by fully repricing the
        book. Nothing here is a recommendation, and no number is a prediction.
      </p>

      <ErrorBanner error={runVar.error ?? runStress.error ?? scenarios.error} />

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Market context</h3>
        <div className="row">
          <div className="field">
            <label htmlFor="rate">Risk-free rate</label>
            <input id="rate" value={rate} onChange={(e) => setRate(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="settle">Settlement time (UTC)</label>
            <input
              id="settle"
              type="time"
              value={settlement}
              onChange={(e) => setSettlement(e.target.value)}
            />
          </div>
        </div>
        <p className="muted" style={{ marginBottom: 0 }}>
          Both runs value the portfolio first, through the same code path as the
          valuation page, and record which valuation they measured.
        </p>
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Value at Risk</h3>
        <div className="row">
          {METHODS.map((item) => (
            <button
              key={item.id}
              onClick={() => setMethod(item.id)}
              className={method === item.id ? "" : "secondary"}
            >
              {item.label}
            </button>
          ))}
        </div>
        <p className="muted">{METHODS.find((m) => m.id === method)?.note}</p>

        <div className="row">
          <div className="field">
            <label htmlFor="horizon">Horizon (days)</label>
            <input id="horizon" value={horizon} onChange={(e) => setHorizon(e.target.value)} />
          </div>
          {method === "MONTE_CARLO" && (
            <>
              <div className="field">
                <label htmlFor="paths">Paths</label>
                <input id="paths" value={paths} onChange={(e) => setPaths(e.target.value)} />
              </div>
              <div className="field">
                <label htmlFor="seed">Seed</label>
                <input id="seed" value={seed} onChange={(e) => setSeed(e.target.value)} />
              </div>
            </>
          )}
          <button
            onClick={() => runVar.mutate()}
            disabled={runVar.isPending}
            style={{ alignSelf: "flex-end" }}
          >
            {runVar.isPending ? "Submitting…" : "Run"}
          </button>
        </div>
        {varStatus.data && (
          <p style={{ marginBottom: 0 }}>
            <SeverityTag
              severity={varStatus.data.status === "FAILED" ? "ERROR" : "INFO"}
            />{" "}
            {varStatus.data.status}
          </p>
        )}
      </div>

      {varEnvelope?.warnings?.length ? (
        <Warnings warnings={varEnvelope.warnings} />
      ) : null}

      {risk && (
        <>
          <div className="row">
            {risk.tail_risk.map((tail) => (
              <Metric
                key={tail.confidence}
                label={`VaR ${(tail.confidence * 100).toFixed(0)}%`}
                value={money(tail.value_at_risk)}
                unit={`threshold loss over ${risk.horizon_days} day(s)`}
              />
            ))}
            {risk.tail_risk.map((tail) => (
              <Metric
                key={`es-${tail.confidence}`}
                label={`Expected shortfall ${(tail.confidence * 100).toFixed(0)}%`}
                value={money(tail.expected_shortfall)}
                unit="average loss when the threshold is exceeded"
              />
            ))}
          </div>

          <div className="card">
            <h3 style={{ marginTop: 0 }}>What this number rests on</h3>
            <div className="grid">
              <div>
                <div className="muted">Method</div>
                <div style={{ fontSize: 20 }}>{risk.method}</div>
              </div>
              <div>
                <div className="muted">Repricing</div>
                <div style={{ fontSize: 20 }}>
                  {String(risk.assumptions.repricing)}
                </div>
              </div>
              <div>
                <div className="muted">Scenarios repriced</div>
                <div style={{ fontSize: 20 }}>{risk.scenarios.toLocaleString()}</div>
              </div>
              <div>
                <div className="muted">Aligned observations</div>
                <div style={{ fontSize: 20 }}>{risk.factor_panel.observations}</div>
              </div>
            </div>

            <table style={{ marginTop: 16 }}>
              <thead>
                <tr>
                  <th>Confidence</th>
                  <th>VaR</th>
                  <th>Expected shortfall</th>
                  <th>Tail observations</th>
                  <th>Reliable?</th>
                  <th>90% interval on the estimate</th>
                </tr>
              </thead>
              <tbody>
                {risk.tail_risk.map((tail) => {
                  const interval =
                    risk.estimate_intervals[tail.confidence.toFixed(2)];
                  return (
                    <tr key={tail.confidence}>
                      <td>{(tail.confidence * 100).toFixed(0)}%</td>
                      <td>{money(tail.value_at_risk)}</td>
                      <td>{money(tail.expected_shortfall)}</td>
                      <td>{tail.tail_observations}</td>
                      <td>
                        <span className={`tag ${tail.is_reliable ? "good" : "warn"}`}>
                          {tail.is_reliable ? "yes" : "thin"}
                        </span>
                      </td>
                      <td>
                        {interval
                          ? `${money(interval.low)} – ${money(interval.high)}`
                          : "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>

            <p className="muted" style={{ marginTop: 12 }}>
              {risk.tail_risk[0]?.interpretation.value_at_risk}{" "}
              {risk.tail_risk[0]?.interpretation.expected_shortfall}
            </p>
            <p className="muted" style={{ marginBottom: 0 }}>
              {risk.factor_panel.missing_data_policy}
            </p>
          </div>

          {risk.worst_scenario_dates.length > 0 && (
            <div className="card">
              <h3 style={{ marginTop: 0 }}>The recorded days that hurt most</h3>
              <ul className="reasons">
                {risk.worst_scenario_dates.map((day) => (
                  <li key={day} className="mono">
                    {day}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Stress lab</h3>
        <div className="row">
          <div className="field" style={{ flex: 1 }}>
            <label htmlFor="scenario">Scenario</label>
            <select
              id="scenario"
              value={scenario}
              style={{ width: "100%" }}
              onChange={(event) => setScenario(event.target.value)}
            >
              {(scenarios.data ?? []).map((item) => (
                <option key={item.id} value={item.name}>
                  {item.name} · {item.source.toLowerCase().replace(/_/g, " ")}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="decay">Time decay (days)</label>
            <input id="decay" value={decay} onChange={(e) => setDecay(e.target.value)} />
          </div>
          <button
            onClick={() => runStress.mutate()}
            disabled={runStress.isPending}
            style={{ alignSelf: "flex-end" }}
          >
            {runStress.isPending ? "Submitting…" : "Apply"}
          </button>
        </div>

        {selected && (
          <>
            <p className="muted" style={{ marginBottom: 4 }}>
              {selected.description}
            </p>
            <p style={{ marginTop: 0 }}>
              {selected.shocks.map((shock) => (
                <span key={`${shock.kind}-${shock.label}`} className="tag info">
                  {shock.kind.toLowerCase().replace(/_/g, " ")} {shock.label}
                </span>
              ))}
            </p>
            {selected.derivation && (
              <p className="muted" style={{ marginBottom: 0 }}>
                Derived from {selected.derivation.series}:{" "}
                {selected.derivation.observations} observations between{" "}
                {selected.derivation.start_date} and {selected.derivation.end_date}; the
                move is the one that occurred on {selected.derivation.event_date}.
              </p>
            )}
          </>
        )}
        {stressStatus.data && (
          <p style={{ marginBottom: 0 }}>
            <SeverityTag
              severity={stressStatus.data.status === "FAILED" ? "ERROR" : "INFO"}
            />{" "}
            {stressStatus.data.status}
          </p>
        )}
      </div>

      {stressEnvelope?.warnings?.length ? (
        <Warnings warnings={stressEnvelope.warnings} />
      ) : null}

      {stress && (
        <>
          <div className="row">
            <Metric
              label="Scenario P&L"
              value={money(stress.pnl)}
              unit="full repricing"
            />
            <Metric
              label="Value after the shock"
              value={money(stress.shocked_value)}
              unit={`from ${money(stress.base_value)}`}
            />
            <Metric
              label="Greek approximation"
              value={money(stress.greek_approximation.pnl)}
              unit="not the answer — shown for comparison"
            />
            <Metric
              label="Linearisation error"
              value={money(stress.greek_approximation.difference_from_full_revaluation)}
              unit="full minus linear"
            />
          </div>

          <div className="card">
            <h3 style={{ marginTop: 0 }}>Why both numbers are here</h3>
            <p className="muted" style={{ marginTop: 0 }}>
              {stress.greek_approximation.caveat} The estimate uses{" "}
              <span className="mono">{stress.greek_approximation.method}</span>.
            </p>
            {stress.floored_volatilities > 0 && (
              <div className="banner warn">
                {stress.floored_volatilities} position(s) had their shocked
                volatility clipped at the floor. A scenario that drives implied
                volatility to zero has left the region where the pricing model
                means anything.
              </div>
            )}
            {stress.excluded_positions > 0 && (
              <div className="banner warn">
                {stress.excluded_positions} position(s) could not be repriced and
                are outside every number above.
              </div>
            )}
          </div>

          {stress.contributions.map((breakdown) => (
            <div className="card" key={breakdown.dimension}>
              <h3 style={{ marginTop: 0 }}>
                Contribution by {breakdown.dimension.toLowerCase().replace(/_/g, " ")}
              </h3>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>{breakdown.dimension.toLowerCase().replace(/_/g, " ")}</th>
                      <th>Positions</th>
                      <th>Value before</th>
                      <th>Contribution</th>
                      <th>Share</th>
                    </tr>
                  </thead>
                  <tbody>
                    {breakdown.contributions.map((item) => (
                      <tr key={item.key}>
                        <td className="mono">{item.key}</td>
                        <td>{item.positions}</td>
                        <td>{money(item.base_value)}</td>
                        <td>{money(item.contribution)}</td>
                        <td>
                          {item.share === null
                            ? "—"
                            : `${(item.share * 100).toFixed(1)}%`}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {breakdown.ungrouped_positions > 0 && (
                <p className="muted" style={{ marginBottom: 0 }}>
                  {breakdown.ungrouped_positions} position(s) carry no value for
                  this dimension and are outside the table rather than filed
                  under a fabricated key. They account for{" "}
                  {money(breakdown.ungrouped_pnl)}.
                </p>
              )}
            </div>
          ))}

          <div className="card">
            <h3 style={{ marginTop: 0 }}>Positions after the shock</h3>
            <div className="table-wrap" style={{ maxHeight: 380 }}>
              <table>
                <thead>
                  <tr>
                    <th>Contract</th>
                    <th>Value before</th>
                    <th>Value after</th>
                    <th>P&L</th>
                    <th>Shocked spot</th>
                    <th>Shocked vol</th>
                  </tr>
                </thead>
                <tbody>
                  {stress.positions.map((position) => (
                    <tr key={position.position_id}>
                      <td className="mono">{position.canonical_key}</td>
                      <td>{money(position.base_value)}</td>
                      <td>{money(position.shocked_value)}</td>
                      <td>{money(position.pnl)}</td>
                      <td>{position.shocked_spot.toFixed(2)}</td>
                      <td>
                        {position.shocked_volatility === null
                          ? "—"
                          : `${(position.shocked_volatility * 100).toFixed(2)}%`}
                        {position.volatility_was_floored ? " (floored)" : ""}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}

      <div className="card">
        <p style={{ margin: 0 }}>
          <Link href={`/portfolios/${portfolioId}/margin`}>
            Margin and the estimated buffer →
          </Link>
        </p>
      </div>

      <Disclaimer />
    </>
  );
}

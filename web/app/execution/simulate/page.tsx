"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { api } from "@/lib/api";
import { Disclaimer, ErrorBanner, Metric, SeverityTag, Warnings } from "@/components/Ui";
import type {
  Envelope,
  ImpactModelInfo,
  Instrument,
  Job,
  JobResult,
  StrategyComparisonOut,
  StrategyInfo,
} from "@/lib/types";

function money(value: string | number | null | undefined, digits = 2) {
  if (value === null || value === undefined) return "—";
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: digits });
}

export default function SimulatePage() {
  const [instrumentId, setInstrumentId] = useState("");
  const [side, setSide] = useState("BUY");
  const [quantity, setQuantity] = useState("7500");
  const [start, setStart] = useState("2026-09-24T09:20");
  const [end, setEnd] = useState("2026-09-24T15:20");
  const [intervals, setIntervals] = useState("6");
  const [chosen, setChosen] = useState<string[]>(["TWAP"]);
  const [impactModel, setImpactModel] = useState("SquareRootImpactModel");
  const [permanent, setPermanent] = useState("1.0");
  const [temporary, setTemporary] = useState("1.0");
  const [volatility, setVolatility] = useState("0.18");
  const [adv, setAdv] = useState("500000");
  const [lot, setLot] = useState("75");
  const [volumes, setVolumes] = useState("");
  const [spreads, setSpreads] = useState("");
  const [latency, setLatency] = useState("0");
  const [maxAge, setMaxAge] = useState("604800");
  const [jobId, setJobId] = useState<string | null>(null);

  const strategies = useQuery({
    queryKey: ["exec-strategies"],
    queryFn: () => api.get<StrategyInfo[]>("/execution/strategies"),
  });
  const impactModels = useQuery({
    queryKey: ["impact-models"],
    queryFn: () => api.get<ImpactModelInfo[]>("/execution/impact-models"),
  });
  const instruments = useQuery({
    queryKey: ["instruments", "option"],
    queryFn: () =>
      api.get<{ items: Instrument[] }>("/instruments?asset_class=OPTION&limit=50"),
  });

  const numbers = (text: string) =>
    text.trim() === ""
      ? null
      : text
          .split(/[,\s]+/)
          .filter(Boolean)
          .map(Number);

  const run = useMutation({
    mutationFn: async () => {
      const accepted = await api.post<{ job_id: string }>("/execution/simulate", {
        instrument_id: instrumentId,
        side,
        quantity,
        start: new Date(start).toISOString(),
        end: new Date(end).toISOString(),
        intervals: Number(intervals) || 6,
        strategies: chosen,
        impact_model: impactModel,
        permanent_coefficient: Number(permanent) || 0,
        temporary_coefficient: Number(temporary) || 0,
        volatility: Number(volatility) || 0.2,
        average_daily_volume: Number(adv) || 1,
        lot_size: lot,
        expected_volumes: numbers(volumes),
        spreads: numbers(spreads),
        latency_seconds: Number(latency) || 0,
        max_price_age_seconds: Number(maxAge) || null,
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
      query.state.data &&
      ["COMPLETED", "FAILED", "CANCELLED"].includes(query.state.data.status)
        ? false
        : 1000,
  });

  const jobResult = useQuery({
    queryKey: ["job-result", jobId],
    queryFn: () => api.get<JobResult>(`/jobs/${jobId}/result`),
    enabled: job.data?.status === "COMPLETED",
  });

  const envelope = jobResult.data?.result as
    | Envelope<StrategyComparisonOut>
    | undefined;
  const comparison = envelope?.results;
  const selectedModel = impactModels.data?.find((item) => item.name === impactModel);

  const toggle = (name: string) =>
    setChosen((current) =>
      current.includes(name)
        ? current.filter((item) => item !== name)
        : [...current, name],
    );

  return (
    <>
      <h2>Execution simulation</h2>
      <p className="subtitle">
        What different schedules would have paid on a path the market already
        printed. Every number here is a counterfactual estimate: these schedules
        were never executed, and running one would itself have moved the path it
        is priced against.
      </p>

      <ErrorBanner error={run.error ?? strategies.error ?? impactModels.error} />

      <div className="card">
        <h3 style={{ marginTop: 0 }}>The order</h3>
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
          <div className="field">
            <label htmlFor="side">Side</label>
            <select id="side" value={side} onChange={(e) => setSide(e.target.value)}>
              <option value="BUY">BUY</option>
              <option value="SELL">SELL</option>
            </select>
          </div>
          <div className="field">
            <label htmlFor="qty">Quantity</label>
            <input id="qty" value={quantity} onChange={(e) => setQuantity(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="lot">Lot size</label>
            <input id="lot" value={lot} onChange={(e) => setLot(e.target.value)} />
          </div>
        </div>
        <div className="row">
          <div className="field">
            <label htmlFor="start">Window start</label>
            <input
              id="start"
              type="datetime-local"
              value={start}
              onChange={(e) => setStart(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="end">Window end</label>
            <input
              id="end"
              type="datetime-local"
              value={end}
              onChange={(e) => setEnd(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="ivals">Intervals</label>
            <input id="ivals" value={intervals} onChange={(e) => setIntervals(e.target.value)} />
          </div>
        </div>
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Strategies</h3>
        <div className="row">
          {(strategies.data ?? []).map((item) => (
            <button
              key={item.name}
              onClick={() => toggle(item.name)}
              className={chosen.includes(item.name) ? "" : "secondary"}
            >
              {item.name}
            </button>
          ))}
        </div>
        <ul className="reasons">
          {(strategies.data ?? [])
            .filter((item) => chosen.includes(item.name))
            .map((item) => (
              <li key={item.name}>
                <span className="mono">{item.name}</span> — {item.description}
                {item.requires.length > 0 ? (
                  <>
                    {" "}
                    Needs: <span className="muted">{item.requires.join(", ")}</span>
                  </>
                ) : (
                  <span className="muted"> Needs nothing but the interval count.</span>
                )}
              </li>
            ))}
        </ul>
        <p className="muted" style={{ marginBottom: 0 }}>
          A strategy whose inputs are missing reports itself unavailable with a
          reason rather than quietly becoming another strategy. This platform has
          no intraday volume profile of its own, so a VWAP or POV schedule needs
          one from you.
        </p>
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Market inputs and impact</h3>
        <div className="row">
          <div className="field" style={{ flex: 1 }}>
            <label htmlFor="model">Impact model</label>
            <select
              id="model"
              value={impactModel}
              style={{ width: "100%" }}
              onChange={(event) => setImpactModel(event.target.value)}
            >
              {(impactModels.data ?? []).map((item) => (
                <option key={item.name} value={item.name}>
                  {item.name} v{item.version}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="perm">eta (permanent)</label>
            <input id="perm" value={permanent} onChange={(e) => setPermanent(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="temp">gamma (temporary)</label>
            <input id="temp" value={temporary} onChange={(e) => setTemporary(e.target.value)} />
          </div>
        </div>
        {selectedModel && (
          <p className="muted" style={{ marginTop: 0 }}>
            {selectedModel.description}{" "}
            <span className="tag warn">no calibrated coefficient shipped</span>
          </p>
        )}
        <p className="muted">
          The coefficients default to one, which is the identity and not a
          calibration: at that setting the impact figures are the shape of the
          model in units of <span className="mono">sigma·sqrt(Q/ADV)</span>, not a
          magnitude anyone measured. Supply values fitted to your own executions
          and the uncalibrated flag clears.
        </p>

        <div className="row">
          <div className="field">
            <label htmlFor="vol">Volatility</label>
            <input id="vol" value={volatility} onChange={(e) => setVolatility(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="adv">Average daily volume</label>
            <input id="adv" value={adv} onChange={(e) => setAdv(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="lat">Latency (seconds)</label>
            <input id="lat" value={latency} onChange={(e) => setLatency(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="age">Max price age (seconds)</label>
            <input id="age" value={maxAge} onChange={(e) => setMaxAge(e.target.value)} />
          </div>
        </div>
        <div className="row">
          <div className="field" style={{ flex: 1 }}>
            <label htmlFor="vols">Expected volume per interval</label>
            <input
              id="vols"
              placeholder="30000 12000 8000 7000 9000 25000"
              value={volumes}
              onChange={(e) => setVolumes(e.target.value)}
            />
          </div>
          <div className="field" style={{ flex: 1 }}>
            <label htmlFor="spr">Spread per interval</label>
            <input
              id="spr"
              placeholder="1 2 4 4 2 1"
              value={spreads}
              onChange={(e) => setSpreads(e.target.value)}
            />
          </div>
        </div>
        <p className="muted" style={{ marginTop: 0 }}>
          A slice whose nearest observation is older than the max price age is
          left unfilled rather than filled at a stale price. Filling a
          hypothetical order against an hours-old quote would assert liquidity
          nobody saw.
        </p>
        <button
          onClick={() => run.mutate()}
          disabled={!instrumentId || chosen.length === 0 || run.isPending}
        >
          {run.isPending ? "Submitting…" : "Simulate"}
        </button>
        {job.data && (
          <p style={{ marginBottom: 0 }}>
            <SeverityTag severity={job.data.status === "FAILED" ? "ERROR" : "INFO"} />{" "}
            {job.data.status}
          </p>
        )}
      </div>

      {envelope?.warnings?.length ? <Warnings warnings={envelope.warnings} /> : null}

      {comparison && (
        <>
          <div className="banner warn">{comparison.caveat}</div>

          <div className="card">
            <h3 style={{ marginTop: 0 }}>Schedules side by side</h3>
            <p className="muted" style={{ marginTop: 0 }}>
              {comparison.comparison_caveat}
            </p>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Strategy</th>
                    <th>Slices</th>
                    <th>Filled</th>
                    <th>Completion</th>
                    <th>Average price</th>
                    <th>Shortfall (bps)</th>
                    <th>Modelled impact</th>
                    <th>Modelled spread</th>
                  </tr>
                </thead>
                <tbody>
                  {comparison.strategies.map((item) => (
                    <tr key={item.strategy}>
                      <td className="mono">{item.strategy}</td>
                      <td>{item.schedule.slices}</td>
                      <td>{item.filled_quantity}</td>
                      <td>
                        <span
                          className={`tag ${item.completion_rate >= 1 ? "good" : "warn"}`}
                        >
                          {(item.completion_rate * 100).toFixed(0)}%
                        </span>
                      </td>
                      <td>{money(item.average_price)}</td>
                      <td>
                        {item.shortfall
                          ? item.shortfall.basis_points.toFixed(1)
                          : "—"}
                      </td>
                      <td>{money(item.modelled_impact_cost)}</td>
                      <td>{money(item.modelled_spread_cost)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {comparison.unavailable.length > 0 && (
            <div className="card">
              <h3 style={{ marginTop: 0 }}>Strategies that could not run</h3>
              <ul className="reasons">
                {comparison.unavailable.map((item) => (
                  <li key={item.strategy}>
                    <span className="mono">{item.strategy}</span> — {item.reason}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {comparison.strategies.map((item) => (
            <div className="card" key={`detail-${item.strategy}`}>
              <h3 style={{ marginTop: 0 }}>{item.strategy}</h3>
              <div className="row">
                <Metric
                  label="Average price"
                  value={money(item.average_price)}
                  unit="counterfactual"
                />
                <Metric
                  label="Modelled impact"
                  value={money(item.modelled_impact_cost)}
                  unit={item.impact_model}
                />
                <Metric
                  label="Time to completion"
                  value={
                    item.time_to_completion_seconds === null
                      ? "—"
                      : `${Math.round(item.time_to_completion_seconds / 60)} min`
                  }
                />
              </div>

              <ul className="reasons">
                {item.schedule.assumptions.map((assumption) => (
                  <li key={assumption}>{assumption}</li>
                ))}
              </ul>

              {item.unfilled.length > 0 && (
                <>
                  <h3>Unfilled slices</h3>
                  <div className="table-wrap" style={{ maxHeight: 200 }}>
                    <table>
                      <thead>
                        <tr>
                          <th>Slice</th>
                          <th>Quantity</th>
                          <th>Why not</th>
                        </tr>
                      </thead>
                      <tbody>
                        {item.unfilled.map((slice) => (
                          <tr key={slice.index}>
                            <td>{slice.index}</td>
                            <td>{slice.quantity}</td>
                            <td className="muted">{slice.reason}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}

              {item.fills && item.fills.length > 0 && (
                <>
                  <h3>Simulated fills</h3>
                  <div className="table-wrap" style={{ maxHeight: 300 }}>
                    <table>
                      <thead>
                        <tr>
                          <th>Slice</th>
                          <th>Time</th>
                          <th>Quantity</th>
                          <th>Observed</th>
                          <th>After drift</th>
                          <th>Fill</th>
                          <th>Spread/unit</th>
                          <th>Impact/unit</th>
                        </tr>
                      </thead>
                      <tbody>
                        {item.fills.map((fill) => (
                          <tr key={fill.index}>
                            <td>{fill.index}</td>
                            <td>{new Date(fill.timestamp).toLocaleTimeString()}</td>
                            <td>{fill.quantity}</td>
                            <td>{money(fill.observed_price, 4)}</td>
                            <td>{money(fill.drifted_price, 4)}</td>
                            <td>{money(fill.fill_price, 4)}</td>
                            <td>{money(fill.spread_cost_per_unit, 4)}</td>
                            <td>
                              {money(
                                Number(fill.temporary_impact_per_unit) +
                                  Number(fill.permanent_impact_per_unit),
                                4,
                              )}
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
        </>
      )}

      <div className="card">
        <p style={{ margin: 0 }}>
          <Link href="/execution">Back to trade analysis →</Link>
        </p>
      </div>

      <Disclaimer />
    </>
  );
}

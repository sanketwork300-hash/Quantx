"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import { Disclaimer, ErrorBanner } from "@/components/Ui";
import type {
  EquityPoint,
  ExperimentDetail,
  ExperimentSummary,
  Instrument,
  Job,
  StrategyDescriptor,
} from "@/lib/types";

function pct(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

function num(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined) return "—";
  return value.toFixed(digits);
}

/**
 * The single most important label on the page. A gross return and a net return
 * are different numbers, and a reader who cannot tell which one they are looking
 * at is being misled by omission.
 */
function CostBadge({ gross }: { gross: boolean }) {
  return gross ? (
    <span className="tag warn">gross of costs</span>
  ) : (
    <span className="tag good">net of supplied costs</span>
  );
}

function ExperimentRow({
  experiment,
  onSelect,
}: {
  experiment: ExperimentSummary;
  onSelect: (id: string) => void;
}) {
  return (
    <tr>
      <td>
        <button className="secondary" onClick={() => onSelect(experiment.id)}>
          {experiment.name}
        </button>
        <div className="muted" style={{ fontSize: 11 }}>
          {experiment.strategy_name} · {experiment.bars_in} bars ·{" "}
          {experiment.fill_count} fills
        </div>
      </td>
      <td className="mono">{pct(experiment.total_return)}</td>
      <td className="mono">{pct(experiment.cagr)}</td>
      <td className="mono">{num(experiment.sharpe)}</td>
      <td className="mono">{pct(experiment.max_drawdown)}</td>
      <td>
        <CostBadge gross={experiment.gross_of_costs} />
      </td>
    </tr>
  );
}

export default function ResearchPage() {
  const queryClient = useQueryClient();
  const [instrumentId, setInstrumentId] = useState("");
  const [strategyName, setStrategyName] = useState("moving_average_crossover");
  const [fast, setFast] = useState("10");
  const [slow, setSlow] = useState("30");
  const [brokerage, setBrokerage] = useState("");
  const [slippage, setSlippage] = useState("");
  const [selected, setSelected] = useState<string | null>(null);

  const strategies = useQuery({
    queryKey: ["strategies"],
    queryFn: () => api.get<StrategyDescriptor[]>("/research/strategies"),
  });

  const instruments = useQuery({
    queryKey: ["research-instruments"],
    queryFn: () => api.get<{ items: Instrument[] }>("/instruments?limit=200"),
  });

  const experiments = useQuery({
    queryKey: ["experiments"],
    queryFn: () => api.get<ExperimentSummary[]>("/research/experiments"),
  });

  const detail = useQuery({
    queryKey: ["experiment", selected],
    enabled: selected !== null,
    queryFn: () => api.get<ExperimentDetail>(`/research/experiments/${selected}`),
  });

  const curve = useQuery({
    queryKey: ["equity-curve", selected],
    enabled: selected !== null,
    queryFn: () =>
      api.get<{ items: EquityPoint[]; count: number }>(
        `/research/experiments/${selected}/equity-curve`,
      ),
  });

  const runIt = useMutation({
    mutationFn: async () => {
      const parameters: Record<string, unknown> =
        strategyName === "moving_average_crossover"
          ? { fast: Number(fast), slow: Number(slow) }
          : {};
      const job = await api.post<{ job_id: string }>("/research/backtests", {
        name: `${strategyName} on ${instrumentId.slice(0, 8)}`,
        instrument_id: instrumentId,
        strategy_name: strategyName,
        strategy_parameters: parameters,
        cost_components: brokerage
          ? [{ name: "brokerage", basis: "TURNOVER", rate: brokerage }]
          : [],
        cost_schedule_source: brokerage ? "rates entered by the user" : null,
        slippage_basis_points: slippage || null,
        slippage_source: slippage ? "assumed by the user" : null,
      });
      await api.get<Job>(`/jobs/${job.job_id}`);
      return job;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["experiments"] }),
  });

  const selectedStrategy = strategies.data?.find((s) => s.name === strategyName);

  return (
    <>
      <h1>Research</h1>
      <p className="subtitle">
        Backtests over warehouse history. A strategy here produces a target
        weight for a <em>simulated</em> book — nothing on this page evaluates a
        strategy against today&rsquo;s market, and nothing here is a
        recommendation.
      </p>

      <ErrorBanner error={runIt.error ?? experiments.error} />

      <form
        className="card"
        onSubmit={(event) => {
          event.preventDefault();
          runIt.mutate();
        }}
      >
        <h2 style={{ marginTop: 0 }}>Run a backtest</h2>
        <div className="row">
          <div className="field">
            <label htmlFor="instrument">Instrument</label>
            <select
              id="instrument"
              value={instrumentId}
              required
              onChange={(event) => setInstrumentId(event.target.value)}
            >
              <option value="">Select…</option>
              {(instruments.data?.items ?? []).map((item) => (
                <option key={item.id} value={item.id}>
                  {item.symbol} ({item.exchange})
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="strategy">Strategy</label>
            <select
              id="strategy"
              value={strategyName}
              onChange={(event) => setStrategyName(event.target.value)}
            >
              {(strategies.data ?? []).map((item) => (
                <option key={item.name} value={item.name}>
                  {item.name.replace(/_/g, " ")}
                </option>
              ))}
            </select>
            {selectedStrategy ? (
              <div className="muted" style={{ fontSize: 11 }}>
                uses {selectedStrategy.features.join(", ") || "no features"}
              </div>
            ) : null}
          </div>
        </div>

        {strategyName === "moving_average_crossover" ? (
          <div className="row">
            <div className="field">
              <label htmlFor="fast">Fast window</label>
              <input id="fast" value={fast} onChange={(e) => setFast(e.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="slow">Slow window</label>
              <input id="slow" value={slow} onChange={(e) => setSlow(e.target.value)} />
            </div>
          </div>
        ) : null}

        <div className="row">
          <div className="field">
            <label htmlFor="brokerage">Brokerage (fraction of turnover)</label>
            <input
              id="brokerage"
              placeholder="e.g. 0.0003"
              value={brokerage}
              onChange={(event) => setBrokerage(event.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="slippage">Slippage (basis points)</label>
            <input
              id="slippage"
              placeholder="e.g. 5"
              value={slippage}
              onChange={(event) => setSlippage(event.target.value)}
            />
          </div>
        </div>
        <p className="muted" style={{ fontSize: 11 }}>
          Leave these empty and the run is <strong>gross</strong>: this platform
          does not hold brokerage, exchange or statutory rates and will not
          invent them. A backtest that quietly assumed free trading is the
          commonest way a strategy reports returns that do not exist.
        </p>

        <button type="submit" disabled={runIt.isPending || !instrumentId}>
          {runIt.isPending ? "Running…" : "Run"}
        </button>
      </form>

      <div className="card">
        <h2 style={{ marginTop: 0 }}>Experiments</h2>
        {experiments.data?.length ? (
          <table>
            <thead>
              <tr>
                <th>Run</th>
                <th>Return</th>
                <th>CAGR</th>
                <th>Sharpe</th>
                <th>Max DD</th>
                <th>Costs</th>
              </tr>
            </thead>
            <tbody>
              {experiments.data.map((experiment) => (
                <ExperimentRow
                  key={experiment.id}
                  experiment={experiment}
                  onSelect={setSelected}
                />
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">Nothing run yet.</p>
        )}
      </div>

      {detail.data ? (
        <div className="card">
          <h2 style={{ marginTop: 0 }}>
            {detail.data.name} <CostBadge gross={detail.data.gross_of_costs} />
          </h2>

          <h3>What the run assumed</h3>
          <table>
            <tbody>
              <tr>
                <td>Strategy</td>
                <td className="mono">
                  {detail.data.strategy_name}@{detail.data.strategy_version}{" "}
                  {JSON.stringify(detail.data.strategy_parameters)}
                </td>
              </tr>
              <tr>
                <td>Features</td>
                <td className="mono">{detail.data.features.join(", ") || "none"}</td>
              </tr>
              <tr>
                <td>Fill timing</td>
                <td className="mono">
                  {String(detail.data.engine_config.timing)}
                  <div className="muted" style={{ fontSize: 11 }}>
                    A decision on one bar is filled on the next. There is no
                    same-bar option.
                  </div>
                </td>
              </tr>
              <tr>
                <td>Cost schedule</td>
                <td className="muted">
                  {String(detail.data.cost_schedule.source ?? "—")}
                </td>
              </tr>
              <tr>
                <td>Slippage</td>
                <td className="muted">
                  {String(detail.data.slippage_model.source ?? "—")}
                </td>
              </tr>
              <tr>
                <td>Code commit</td>
                <td className="mono">{detail.data.code_commit}</td>
              </tr>
            </tbody>
          </table>

          <h3>Performance</h3>
          <p className="muted">
            Annualised on {num(detail.data.metrics.periods_per_year, 1)} bars per
            year, measured from the data rather than assumed.{" "}
            {detail.data.metrics.is_reliable
              ? ""
              : `Only ${detail.data.metrics.observations} return observations — the ratios are reported with that count attached rather than withheld.`}
          </p>
          <table>
            <tbody>
              <tr>
                <td>Total return</td>
                <td className="mono">{pct(detail.data.metrics.total_return)}</td>
              </tr>
              <tr>
                <td>CAGR</td>
                <td className="mono">{pct(detail.data.metrics.cagr)}</td>
              </tr>
              <tr>
                <td>Volatility (ann.)</td>
                <td className="mono">
                  {pct(detail.data.metrics.annualised_volatility)}
                </td>
              </tr>
              <tr>
                <td>Sharpe / Sortino / Calmar</td>
                <td className="mono">
                  {num(detail.data.metrics.sharpe)} /{" "}
                  {num(detail.data.metrics.sortino)} /{" "}
                  {num(detail.data.metrics.calmar)}
                </td>
              </tr>
              <tr>
                <td>Max drawdown</td>
                <td className="mono">
                  {pct(detail.data.metrics.max_drawdown?.depth)}
                  {detail.data.metrics.max_drawdown?.recovery_bars === null ? (
                    <span className="muted"> · never recovered</span>
                  ) : null}
                </td>
              </tr>
              <tr>
                <td>Trades</td>
                <td className="mono">
                  {detail.data.metrics.trades?.count} · win rate{" "}
                  {pct(detail.data.metrics.trades?.win_rate)} · profit factor{" "}
                  {num(detail.data.metrics.trades?.profit_factor)}
                </td>
              </tr>
            </tbody>
          </table>

          <h3>Where the money came from</h3>
          <table>
            <tbody>
              <tr>
                <td>Equity change</td>
                <td className="mono">{detail.data.attribution.equity_change}</td>
              </tr>
              <tr>
                <td>Realised + unrealised</td>
                <td className="mono">
                  {detail.data.attribution.realised_pnl} +{" "}
                  {detail.data.attribution.unrealised_pnl}
                </td>
              </tr>
              <tr>
                <td>Costs</td>
                <td className="mono">−{detail.data.attribution.costs}</td>
              </tr>
              <tr>
                <td>Slippage vs reference</td>
                <td className="mono">
                  {detail.data.attribution.slippage_against_reference}
                  <div className="muted" style={{ fontSize: 11 }}>
                    Already inside the price P&amp;L above — shown to quantify
                    it, not subtracted again.
                  </div>
                </td>
              </tr>
              <tr>
                <td>Residual</td>
                <td className="mono">
                  {detail.data.attribution.residual}{" "}
                  {detail.data.attribution.reconciles ? (
                    <span className="tag good">reconciles</span>
                  ) : (
                    <span className="tag bad">does not reconcile</span>
                  )}
                </td>
              </tr>
            </tbody>
          </table>

          {curve.data ? (
            <p className="muted">
              Equity curve: {curve.data.count} points, from{" "}
              {curve.data.items[0]?.equity} to{" "}
              {curve.data.items[curve.data.count - 1]?.equity}.
            </p>
          ) : null}

          {detail.data.warnings.length ? (
            <>
              <h3>What the run wants you to know</h3>
              {detail.data.warnings.map((warning, index) => (
                <div key={`${warning.code}-${index}`} style={{ marginBottom: 6 }}>
                  <span className="mono">{warning.code}</span>
                  <div className="muted">{warning.message}</div>
                </div>
              ))}
            </>
          ) : null}
        </div>
      ) : null}

      <Disclaimer />
    </>
  );
}

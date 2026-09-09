"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import { Disclaimer, ErrorBanner } from "@/components/Ui";
import type {
  Envelope,
  Instrument,
  OptimisationObjective,
  TargetPortfolio,
} from "@/lib/types";

const OBJECTIVES: { id: OptimisationObjective; label: string; needsReturns: boolean }[] = [
  { id: "MINIMUM_VARIANCE", label: "Minimum variance", needsReturns: false },
  { id: "RISK_PARITY", label: "Risk parity", needsReturns: false },
  { id: "MAXIMUM_SHARPE", label: "Maximum Sharpe", needsReturns: true },
  { id: "MEAN_VARIANCE", label: "Mean-variance", needsReturns: true },
  { id: "MINIMUM_CVAR", label: "Minimum CVaR", needsReturns: false },
];

function pct(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

/**
 * Where the return forecast came from. Two portfolios with the same covariance
 * and different return sources are different objects, so this is the first
 * thing worth reading.
 */
function ReturnSource({ source }: { source: string | null }) {
  if (source === null) return <span className="tag info">no forecast needed</span>;
  if (source === "HISTORICAL_MEAN")
    return <span className="tag warn">historical means</span>;
  return <span className="tag good">{source.toLowerCase().replace(/_/g, " ")}</span>;
}

export default function ConstructPage() {
  const [selected, setSelected] = useState<string[]>([]);
  const [objective, setObjective] = useState<OptimisationObjective>("MINIMUM_VARIANCE");
  const [maxWeight, setMaxWeight] = useState("");
  const [shrink, setShrink] = useState(false);
  const [useHistorical, setUseHistorical] = useState(false);

  const instruments = useQuery({
    queryKey: ["construct-instruments"],
    queryFn: () => api.get<{ items: Instrument[] }>("/instruments?limit=200"),
  });

  const optimise = useMutation({
    mutationFn: () =>
      api.post<Envelope<TargetPortfolio>>("/portfolio-optimisation/target", {
        instrument_ids: selected,
        objective,
        covariance_estimator: shrink ? "LEDOIT_WOLF" : "SAMPLE",
        use_historical_means: useHistorical,
        constraints: {
          budget: 1.0,
          long_only: true,
          maximum_weight: maxWeight ? Number(maxWeight) : null,
        },
      }),
  });

  const chosen = OBJECTIVES.find((item) => item.id === objective);
  const result = optimise.data?.results;

  const toggle = (id: string) =>
    setSelected((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
    );

  return (
    <>
      <h2>Portfolio construction</h2>
      <p className="subtitle">
        A target portfolio from a covariance estimated over warehouse history,
        with the risk of the portfolio that came back reported beside it.
      </p>

      <ErrorBanner error={optimise.error} />

      <form
        className="card"
        onSubmit={(event) => {
          event.preventDefault();
          optimise.mutate();
        }}
      >
        <div className="field">
          <label>Instruments (at least two)</label>
          <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
            {(instruments.data?.items ?? []).slice(0, 40).map((item) => (
              <button
                type="button"
                key={item.id}
                className={selected.includes(item.id) ? "" : "secondary"}
                onClick={() => toggle(item.id)}
              >
                {item.symbol}
              </button>
            ))}
          </div>
        </div>

        <div className="field">
          <label htmlFor="objective">Objective</label>
          <select
            id="objective"
            value={objective}
            onChange={(event) =>
              setObjective(event.target.value as OptimisationObjective)
            }
          >
            {OBJECTIVES.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </select>
          {chosen && !chosen.needsReturns ? (
            <div className="muted" style={{ fontSize: 11 }}>
              Needs no return forecast, which is why it is offered.
            </div>
          ) : (
            <div className="muted" style={{ fontSize: 11 }}>
              Needs expected returns. Without a stated forecast this will be
              refused rather than given sample means.
            </div>
          )}
        </div>

        <div className="row">
          <div className="field">
            <label htmlFor="maxw">Maximum weight per asset</label>
            <input
              id="maxw"
              placeholder="e.g. 0.4"
              value={maxWeight}
              onChange={(event) => setMaxWeight(event.target.value)}
            />
          </div>
        </div>

        <div className="field">
          <label>
            <input
              type="checkbox"
              checked={shrink}
              onChange={(event) => setShrink(event.target.checked)}
            />{" "}
            Shrink the covariance (Ledoit-Wolf)
          </label>
          <div className="muted" style={{ fontSize: 11 }}>
            Asked for by name, never applied automatically. The intensity it
            chooses is reported.
          </div>
        </div>

        {chosen?.needsReturns ? (
          <div className="field">
            <label>
              <input
                type="checkbox"
                checked={useHistorical}
                onChange={(event) => setUseHistorical(event.target.checked)}
              />{" "}
              Use historical means as the forecast
            </label>
            <div className="muted" style={{ fontSize: 11 }}>
              Historical means are a poor forecast, and this optimiser puts the
              most weight exactly where that error is largest. The result will
              say it used them.
            </div>
          </div>
        ) : null}

        <button type="submit" disabled={optimise.isPending || selected.length < 2}>
          {optimise.isPending ? "Solving…" : "Build"}
        </button>
      </form>

      {result ? (
        <>
          <div className="card">
            <h3 style={{ marginTop: 0 }}>
              Target portfolio <ReturnSource source={result.return_source} />
            </h3>
            <table>
              <thead>
                <tr>
                  <th>Instrument</th>
                  <th>Weight</th>
                  <th>Share of risk</th>
                </tr>
              </thead>
              <tbody>
                {result.holdings.map((holding) => (
                  <tr key={holding.instrument_id}>
                    <td>{holding.symbol}</td>
                    <td className="mono">{pct(holding.weight)}</td>
                    {/* Often very different from the weight — and that
                        difference is where the portfolio's real bet is. */}
                    <td className="mono">{pct(holding.risk_contribution)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="card">
            <h3 style={{ marginTop: 0 }}>Risk of this portfolio</h3>
            <table>
              <tbody>
                <tr>
                  <td>Volatility</td>
                  <td className="mono">{pct(result.risk.volatility)}</td>
                </tr>
                <tr>
                  <td>Effective assets</td>
                  <td className="mono">
                    {result.risk.effective_assets.toFixed(2)} of{" "}
                    {result.holdings.length}
                    <div className="muted" style={{ fontSize: 11 }}>
                      {result.risk.interpretation.effective_assets}
                    </div>
                  </td>
                </tr>
                <tr>
                  <td>Largest weight / largest risk share</td>
                  <td className="mono">
                    {pct(result.risk.largest_weight)} /{" "}
                    {pct(result.risk.largest_risk_contribution)}
                  </td>
                </tr>
                <tr>
                  <td>Historical VaR / expected shortfall</td>
                  <td className="mono">
                    {pct(result.risk.tail.value_at_risk)} /{" "}
                    {pct(result.risk.tail.expected_shortfall)}
                    {result.risk.tail.is_reliable === false ? (
                      <span className="tag warn" style={{ marginLeft: 6 }}>
                        thin tail
                      </span>
                    ) : null}
                  </td>
                </tr>
                <tr>
                  <td>Observations</td>
                  <td className="mono">{result.risk.observations}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div className="card">
            <h3 style={{ marginTop: 0 }}>How it was solved</h3>
            <p className="muted">
              Covariance: {String(result.covariance.estimator)} over{" "}
              {String(result.covariance.observations)} observations
              {result.covariance.shrinkage_intensity
                ? `, shrunk with intensity ${Number(result.covariance.shrinkage_intensity).toFixed(3)}`
                : ""}
              . Solver: {String(result.solver.starts_converged ?? "—")} of{" "}
              {String(result.solver.starts_attempted ?? "—")} starts converged.
            </p>
            {Array.isArray(result.solver.binding_constraints) &&
            result.solver.binding_constraints.length ? (
              <p className="muted">
                Sitting on: {result.solver.binding_constraints.join("; ")} — which
                is usually what is preventing something else.
              </p>
            ) : null}
          </div>

          {optimise.data?.warnings.length ? (
            <div className="card">
              <h3 style={{ marginTop: 0 }}>What the run wants you to know</h3>
              {optimise.data.warnings.map((warning, index) => (
                <div key={`${warning.code}-${index}`} style={{ marginBottom: 6 }}>
                  <span className="mono">{warning.code}</span>
                  <div className="muted">{warning.message}</div>
                </div>
              ))}
            </div>
          ) : null}
        </>
      ) : null}

      <Disclaimer />
    </>
  );
}

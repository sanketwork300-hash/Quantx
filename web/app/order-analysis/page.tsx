"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import { Disclaimer, ErrorBanner, SeverityTag, Warnings } from "@/components/Ui";
import type {
  Envelope,
  Instrument,
  Listing,
  Movement,
  OrderAnalysisResults,
  OrderBranch,
  OrderCostOut,
  Portfolio,
  Scenario,
} from "@/lib/types";

const BRANCHES = ["VALUATION", "SURFACE", "EXECUTION", "RISK", "MARGIN"] as const;

const BRANCH_QUESTION: Record<string, string> = {
  VALUATION: "What does the market show, and what do the models say around it?",
  SURFACE: "Is this contract's implied volatility out of line with the surface?",
  EXECUTION: "What is it estimated to cost to execute?",
  RISK: "What does it do to the book's Greeks, VaR and stress loss?",
  MARGIN: "What does it do to the estimated margin and the buffer?",
};

function number(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function StatusTag({ status }: { status: string }) {
  const tone = status === "OK" ? "good" : status === "PARTIAL" ? "warn" : "bad";
  return <span className={`tag ${tone}`}>{status}</span>;
}

/**
 * Before, after, and the difference. The difference is what the page is for,
 * so it is the column that is emphasised — and it is only ever shown when both
 * sides exist, because a change against a missing side is not a change.
 */
function Movements({ movements }: { movements: Movement[] }) {
  if (!movements.length) return <p className="muted">Nothing to compare.</p>;
  return (
    <table>
      <thead>
        <tr>
          <th>Measure</th>
          <th style={{ textAlign: "right" }}>Current</th>
          <th style={{ textAlign: "right" }}>With the order</th>
          <th style={{ textAlign: "right" }}>Change</th>
        </tr>
      </thead>
      <tbody>
        {movements.map((movement) => (
          <tr key={movement.name}>
            <td>
              {movement.name}
              <div className="muted" style={{ fontSize: 11 }}>
                {movement.unit}
              </div>
            </td>
            <td style={{ textAlign: "right" }}>{number(movement.current)}</td>
            <td style={{ textAlign: "right" }}>{number(movement.proposed)}</td>
            <td style={{ textAlign: "right" }}>
              <strong>{number(movement.change)}</strong>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** A branch that could not answer shows why, in place of its numbers. */
function BranchShell({
  name,
  branch,
  children,
}: {
  name: string;
  branch: OrderBranch<unknown> | undefined;
  children: React.ReactNode;
}) {
  if (!branch) return null;
  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>
        {name} <StatusTag status={branch.status} />
      </h3>
      <p className="muted" style={{ marginTop: 0 }}>
        {BRANCH_QUESTION[name]}
      </p>
      {branch.status === "FAILED" ? (
        <ul className="reasons">
          {branch.warnings.map((warning) => (
            <li key={warning.code}>
              <SeverityTag severity={warning.severity} />{" "}
              <span className="mono">{warning.code}</span>
              <div className="muted">{warning.message}</div>
            </li>
          ))}
        </ul>
      ) : (
        children
      )}
      <div className="muted" style={{ fontSize: 11, marginTop: 10 }}>
        snapshot <span className="mono">{branch.provenance.market_state_id}</span>
      </div>
    </div>
  );
}

function ExecutionBranch({ cost }: { cost: OrderCostOut }) {
  return (
    <>
      <p className="muted" style={{ marginTop: 0 }}>
        Reference {cost.reference_price} · quoted spread{" "}
        {cost.quoted_spread ?? "—"} ·{" "}
        <span className={`tag ${cost.marketability === "PASSIVE" ? "warn" : "info"}`}>
          {cost.marketability}
        </span>{" "}
        {cost.marketability_basis}
      </p>
      <table>
        <thead>
          <tr>
            <th>Schedule</th>
            <th style={{ textAlign: "right" }}>Spread</th>
            <th style={{ textAlign: "right" }}>Impact</th>
            <th style={{ textAlign: "right" }}>Estimated slippage</th>
            <th style={{ textAlign: "right" }}>bps</th>
          </tr>
        </thead>
        <tbody>
          {cost.strategies.map((item) => (
            <tr key={item.strategy}>
              <td>{item.strategy}</td>
              <td style={{ textAlign: "right" }}>
                {number(item.spread_component_currency)}
              </td>
              <td style={{ textAlign: "right" }}>
                {number(item.impact_component_currency)}
              </td>
              <td style={{ textAlign: "right" }}>
                <strong>{number(item.estimated_slippage_currency)}</strong>
              </td>
              <td style={{ textAlign: "right" }}>
                {number(item.estimated_slippage_basis_points, 1)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {cost.unavailable.length > 0 && (
        <ul className="reasons">
          {cost.unavailable.map((item) => (
            <li key={item.strategy}>
              <span className="mono">{item.strategy}</span>
              <div className="muted">{item.reason}</div>
            </li>
          ))}
        </ul>
      )}
      <p className="muted">{cost.caveat}</p>
      <p className="muted">{cost.interpretation}</p>
    </>
  );
}

export default function OrderAnalysisPage() {
  const [portfolioId, setPortfolioId] = useState("");
  const [instrumentId, setInstrumentId] = useState("");
  const [side, setSide] = useState("SELL");
  const [quantity, setQuantity] = useState("150");
  const [orderType, setOrderType] = useState("MARKET");
  const [limitPrice, setLimitPrice] = useState("");
  const [rate, setRate] = useState("0.065");
  const [settlement, setSettlement] = useState("10:00");
  const [scenario, setScenario] = useState("");
  const [adv, setAdv] = useState("");
  const [volatility, setVolatility] = useState("");
  const [capital, setCapital] = useState("");

  const portfolios = useQuery({
    queryKey: ["portfolios"],
    queryFn: () => api.get<Portfolio[]>("/portfolios"),
  });

  const instruments = useQuery({
    queryKey: ["instruments", "options"],
    queryFn: () =>
      api.get<Listing<Instrument>>("/instruments?asset_class=OPTION&limit=200"),
  });

  const scenarios = useQuery({
    queryKey: ["scenarios"],
    queryFn: () => api.get<Scenario[]>("/scenarios"),
  });

  const run = useMutation({
    mutationFn: () =>
      api.post<Envelope<OrderAnalysisResults>>("/order-analysis", {
        portfolio_id: portfolioId,
        instrument_id: instrumentId,
        side,
        quantity,
        order_type: orderType,
        limit_price: orderType === "LIMIT" && limitPrice ? limitPrice : null,
        risk_free_rate: rate === "" ? 0 : Number(rate),
        settlement_time_utc: settlement === "" ? null : `${settlement}:00`,
        scenario: scenario === "" ? null : scenario,
        execution: {
          average_daily_volume: adv === "" ? 0 : Number(adv),
          volatility: volatility === "" ? 0 : Number(volatility),
        },
        margin: { eligible_capital: capital === "" ? null : Number(capital) },
      }),
  });

  const envelope = run.data;
  const results = envelope?.results ?? undefined;
  const stateIds = new Set(
    results
      ? BRANCHES.map((name) => results.branches[name]?.provenance.market_state_id)
      : [],
  );

  return (
    <>
      <h2>Order analysis</h2>
      <p className="subtitle">
        One proposed order, five engines, one market snapshot. The
        current-to-proposed differences below are attributable to the order
        because every branch read the same snapshot — not to five calculations
        catching the market at five moments. Nothing here is a recommendation to
        trade or not to trade.
      </p>

      <ErrorBanner error={run.error} />

      <div className="card">
        <h3 style={{ marginTop: 0 }}>The order</h3>
        <div className="row">
          <div className="field" style={{ flex: 2 }}>
            <label htmlFor="portfolio">Portfolio</label>
            <select
              id="portfolio"
              value={portfolioId}
              style={{ width: "100%" }}
              onChange={(event) => setPortfolioId(event.target.value)}
            >
              <option value="">Choose a portfolio</option>
              {(portfolios.data ?? []).map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </div>
          <div className="field" style={{ flex: 3 }}>
            <label htmlFor="instrument">Contract</label>
            <select
              id="instrument"
              value={instrumentId}
              style={{ width: "100%" }}
              onChange={(event) => setInstrumentId(event.target.value)}
            >
              <option value="">Choose a contract</option>
              {(instruments.data?.items ?? []).map((item) => (
                <option key={item.id} value={item.id}>
                  {item.canonical_key}
                </option>
              ))}
            </select>
          </div>
        </div>
        <div className="row">
          <div className="field">
            <label htmlFor="side">Side</label>
            <select
              id="side"
              value={side}
              onChange={(event) => setSide(event.target.value)}
            >
              <option value="BUY">BUY</option>
              <option value="SELL">SELL</option>
            </select>
          </div>
          <div className="field">
            <label htmlFor="quantity">Quantity</label>
            <input
              id="quantity"
              value={quantity}
              onChange={(event) => setQuantity(event.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="order-type">Order type</label>
            <select
              id="order-type"
              value={orderType}
              onChange={(event) => setOrderType(event.target.value)}
            >
              <option value="MARKET">MARKET</option>
              <option value="LIMIT">LIMIT</option>
            </select>
          </div>
          {orderType === "LIMIT" && (
            <div className="field">
              <label htmlFor="limit">Limit price</label>
              <input
                id="limit"
                value={limitPrice}
                onChange={(event) => setLimitPrice(event.target.value)}
              />
            </div>
          )}
        </div>
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>What the platform does not hold</h3>
        <p className="muted" style={{ marginTop: 0 }}>
          Average daily volume and volatility are yours to supply. Left empty,
          the impact half of the cost estimate is reported as absent rather than
          as zero, and no total slippage is stated. Eligible capital works the
          same way for the margin buffer.
        </p>
        <div className="row">
          <div className="field">
            <label htmlFor="rate">Risk-free rate</label>
            <input
              id="rate"
              value={rate}
              onChange={(event) => setRate(event.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="settlement">Settlement (UTC)</label>
            <input
              id="settlement"
              type="time"
              value={settlement}
              onChange={(event) => setSettlement(event.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="adv">Average daily volume</label>
            <input
              id="adv"
              value={adv}
              placeholder="not held"
              onChange={(event) => setAdv(event.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="volatility">Volatility for impact</label>
            <input
              id="volatility"
              value={volatility}
              placeholder="not held"
              onChange={(event) => setVolatility(event.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="capital">Eligible capital</label>
            <input
              id="capital"
              value={capital}
              placeholder="unknown"
              onChange={(event) => setCapital(event.target.value)}
            />
          </div>
          <div className="field" style={{ flex: 1 }}>
            <label htmlFor="scenario">Stress scenario</label>
            <select
              id="scenario"
              value={scenario}
              style={{ width: "100%" }}
              onChange={(event) => setScenario(event.target.value)}
            >
              <option value="">No stress comparison</option>
              {(scenarios.data ?? []).map((item) => (
                <option key={item.id} value={item.name}>
                  {item.name}
                </option>
              ))}
            </select>
          </div>
        </div>
        <button
          onClick={() => run.mutate()}
          disabled={!portfolioId || !instrumentId || run.isPending}
        >
          {run.isPending ? "Analysing…" : "Analyse"}
        </button>
      </div>

      {envelope && results && (
        <>
          <div className="card">
            <h3 style={{ marginTop: 0 }}>
              {results.order.canonical_key} · {results.order.side}{" "}
              {results.order.quantity} <StatusTag status={envelope.status} />
            </h3>
            <p className="muted" style={{ marginTop: 0 }}>
              {results.counts.ok} of {BRANCHES.length} branches answered.
            </p>
            <table>
              <tbody>
                <tr>
                  <td>Market snapshot</td>
                  <td className="mono">{results.market_state.state_id}</td>
                </tr>
                <tr>
                  <td>As of</td>
                  <td>{results.market_state.as_of_timestamp}</td>
                </tr>
                <tr>
                  <td>Read by</td>
                  <td>
                    {stateIds.size === 1 ? (
                      <span className="tag good">all five branches</span>
                    ) : (
                      <span className="tag bad">
                        {stateIds.size} different snapshots
                      </span>
                    )}
                  </td>
                </tr>
              </tbody>
            </table>
            <p className="muted">{results.interpretation}</p>
          </div>

          <BranchShell name="RISK" branch={results.branches.RISK}>
            <h4>Greeks</h4>
            <Movements
              movements={results.branches.RISK.results?.greeks.movements ?? []}
            />
            {results.branches.RISK.results?.value_at_risk ? (
              <>
                <h4>
                  Value at risk and expected shortfall (
                  {results.branches.RISK.results.value_at_risk.method})
                </h4>
                <Movements
                  movements={results.branches.RISK.results.value_at_risk.movements}
                />
              </>
            ) : (
              <p className="muted">
                No value-at-risk comparison: the reason is in the warnings below.
              </p>
            )}
            {results.branches.RISK.results?.stress && (
              <>
                <h4>
                  Scenario — {results.branches.RISK.results.stress.scenario}
                </h4>
                <Movements movements={results.branches.RISK.results.stress.movements} />
              </>
            )}
          </BranchShell>

          <BranchShell name="MARGIN" branch={results.branches.MARGIN}>
            <Movements movements={results.branches.MARGIN.results?.movements ?? []} />
            <p className="muted">{results.branches.MARGIN.results?.disclaimer}</p>
          </BranchShell>

          <BranchShell name="EXECUTION" branch={results.branches.EXECUTION}>
            {results.branches.EXECUTION.results && (
              <ExecutionBranch cost={results.branches.EXECUTION.results} />
            )}
          </BranchShell>

          <BranchShell name="VALUATION" branch={results.branches.VALUATION}>
            <pre className="mono" style={{ fontSize: 11, overflow: "auto" }}>
              {JSON.stringify(results.branches.VALUATION.results, null, 2)}
            </pre>
          </BranchShell>

          <BranchShell name="SURFACE" branch={results.branches.SURFACE}>
            <pre className="mono" style={{ fontSize: 11, overflow: "auto" }}>
              {JSON.stringify(results.branches.SURFACE.results, null, 2)}
            </pre>
          </BranchShell>

          <Warnings warnings={envelope.warnings} />
        </>
      )}

      <Disclaimer />
    </>
  );
}

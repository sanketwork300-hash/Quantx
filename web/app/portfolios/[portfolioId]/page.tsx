"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import { Disclaimer, ErrorBanner, Metric, SeverityTag, Warnings } from "@/components/Ui";
import type {
  Envelope,
  Job,
  JobResult,
  Portfolio,
  PortfolioValuation,
  Position,
  ValuationDetail,
} from "@/lib/types";

const DIMENSIONS = ["UNDERLYING", "EXPIRY", "ASSET_CLASS", "STRATEGY_TAG", "CURRENCY"];

/** Marked to market or marked to model: the difference is never cosmetic. */
function MethodTag({ method }: { method: string }) {
  const tone =
    method === "UNAVAILABLE"
      ? "bad"
      : method === "STALE_MARKET" || method === "MODEL_REFERENCE"
        ? "warn"
        : "good";
  return <span className={`tag ${tone}`}>{method}</span>;
}

function money(value: string | null) {
  if (value === null) return "—";
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 });
}

export default function PortfolioPage() {
  const { portfolioId } = useParams<{ portfolioId: string }>();
  const queryClient = useQueryClient();
  const [rate, setRate] = useState("0.065");
  const [settlement, setSettlement] = useState("10:00");
  const [dimension, setDimension] = useState("UNDERLYING");
  const [jobId, setJobId] = useState<string | null>(null);

  const portfolio = useQuery({
    queryKey: ["portfolio", portfolioId],
    queryFn: () => api.get<Portfolio>(`/portfolios/${portfolioId}`),
  });

  const positions = useQuery({
    queryKey: ["positions", portfolioId],
    queryFn: () => api.get<Position[]>(`/portfolios/${portfolioId}/positions`),
  });

  const latest = useQuery({
    queryKey: ["valuation", portfolioId],
    queryFn: async () => {
      try {
        return await api.get<PortfolioValuation>(`/portfolios/${portfolioId}/valuation`);
      } catch (error) {
        // Not yet valued is a state, not a failure.
        if (error instanceof ApiError && error.problem.status === 404) return null;
        throw error;
      }
    },
  });

  const detail = useQuery({
    queryKey: ["valuation-detail", latest.data?.valuation_id],
    queryFn: () =>
      api.get<Envelope<ValuationDetail>>(
        `/portfolios/${portfolioId}/valuation/${latest.data!.valuation_id}`,
      ),
    enabled: Boolean(latest.data?.valuation_id),
  });

  const value = useMutation({
    mutationFn: async () => {
      const accepted = await api.post<{ job_id: string }>(
        `/portfolios/${portfolioId}/valuation`,
        {
          risk_free_rate: rate === "" ? 0 : Number(rate),
          dividend_yield: 0,
          settlement_time_utc: settlement === "" ? null : `${settlement}:00`,
        },
      );
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
    queryFn: async () => {
      const result = await api.get<JobResult>(`/jobs/${jobId}/result`);
      queryClient.invalidateQueries({ queryKey: ["valuation", portfolioId] });
      return result;
    },
    enabled: job.data?.status === "COMPLETED",
  });

  const valuation = latest.data;
  const buckets = (valuation?.aggregates ?? []).filter((b) => b.dimension === dimension);
  const envelope = jobResult.data?.result as Envelope<unknown> | undefined;

  return (
    <>
      <h2>{portfolio.data?.name ?? "Portfolio"}</h2>
      <p className="subtitle">
        Positions are valued against one market snapshot. Each one records the
        price it used and where that price came from.
      </p>

      <ErrorBanner error={positions.error ?? latest.error ?? value.error} />

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Value this portfolio</h3>
        <div className="row">
          <div className="field">
            <label htmlFor="rate">Risk-free rate</label>
            <input id="rate" value={rate} onChange={(event) => setRate(event.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="settle">Settlement time (UTC)</label>
            <input
              id="settle"
              type="time"
              value={settlement}
              onChange={(event) => setSettlement(event.target.value)}
            />
          </div>
          <button
            onClick={() => value.mutate()}
            disabled={value.isPending}
            style={{ alignSelf: "flex-end" }}
          >
            {value.isPending ? "Submitting…" : "Value"}
          </button>
        </div>
        <p className="muted" style={{ marginBottom: 0 }}>
          Without a settlement time, time to expiry is undefined, so option
          Greeks are omitted rather than computed against a guessed moment.
        </p>
        {job.data && (
          <p style={{ marginBottom: 0 }}>
            <span className="mono">{job.data.job_type}</span>{" "}
            <SeverityTag severity={job.data.status === "FAILED" ? "ERROR" : "INFO"} />{" "}
            {job.data.status}
          </p>
        )}
      </div>

      {envelope?.warnings?.length ? <Warnings warnings={envelope.warnings} /> : null}

      {valuation && (
        <>
          <div className="row">
            <Metric
              label="Net exposure"
              value={money(valuation.net_exposure)}
              unit={valuation.base_currency}
            />
            <Metric
              label="Gross exposure"
              value={money(valuation.gross_exposure)}
              unit={valuation.base_currency}
            />
            <Metric
              label="Unrealised P&L"
              value={money(valuation.unrealized_pnl)}
              unit={`${valuation.base_currency}, against average entry price`}
            />
            <Metric
              label="Valued"
              value={`${valuation.valued} / ${valuation.positions}`}
              unit="positions with a usable price"
            />
          </div>

          <div className="card">
            <h3 style={{ marginTop: 0 }}>Portfolio Greeks</h3>
            <div className="grid">
              {(
                [
                  "delta",
                  "gamma",
                  "vega_per_vol_point",
                  "theta_per_day",
                  "rho_per_bp",
                ] as const
              ).map((name) => (
                <div key={name}>
                  <div className="muted">{name}</div>
                  <div style={{ fontSize: 20 }}>
                    {valuation.greeks[name].toLocaleString(undefined, {
                      maximumFractionDigits: 2,
                    })}
                  </div>
                  <div className="muted" style={{ fontSize: 12 }}>
                    {valuation.greeks.units?.[name]}
                  </div>
                </div>
              ))}
            </div>
            <p className="muted" style={{ marginBottom: 0 }}>
              Snapshot <span className="mono">{valuation.market_state_id}</span> ·{" "}
              {new Date(valuation.as_of_timestamp).toLocaleString()}
            </p>
          </div>

          <div className="card">
            <h3 style={{ marginTop: 0 }}>How each position was priced</h3>
            <div className="row">
              {Object.entries(valuation.valuation_methods).map(([method, count]) => (
                <div key={method}>
                  <MethodTag method={method} /> <strong>{count}</strong>
                </div>
              ))}
            </div>
            <p className="muted" style={{ marginBottom: 0 }}>
              A model price is an estimate from the fitted surface, not an
              observation. Both are stored, and neither is ever written from the
              other.
            </p>
          </div>

          <div className="card">
            <h3 style={{ marginTop: 0 }}>Grouped</h3>
            <div className="row">
              {DIMENSIONS.map((name) => (
                <button
                  key={name}
                  onClick={() => setDimension(name)}
                  className={dimension === name ? "" : "secondary"}
                >
                  {name.replace("_", " ").toLowerCase()}
                </button>
              ))}
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>{dimension.replace("_", " ").toLowerCase()}</th>
                    <th>Positions</th>
                    <th>Net</th>
                    <th>Gross</th>
                    <th>Delta</th>
                    <th>Gamma</th>
                    <th>Vega / vol pt</th>
                    <th>Theta / day</th>
                  </tr>
                </thead>
                <tbody>
                  {buckets.map((bucket) => (
                    <tr key={`${bucket.dimension}-${bucket.key}`}>
                      <td>{bucket.label}</td>
                      <td>
                        {bucket.valued} / {bucket.positions}
                      </td>
                      <td>{money(bucket.net_exposure)}</td>
                      <td>{money(bucket.gross_exposure)}</td>
                      <td>{bucket.greeks.delta.toFixed(2)}</td>
                      <td>{bucket.greeks.gamma.toFixed(4)}</td>
                      <td>{bucket.greeks.vega_per_vol_point.toFixed(2)}</td>
                      <td>{bucket.greeks.theta_per_day.toFixed(2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {buckets.length === 0 && (
              <p className="muted">
                No position carries this dimension, so nothing is grouped under a
                fabricated key.
              </p>
            )}
          </div>
        </>
      )}

      {detail.data?.results && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Positions, as valued</h3>
          <div className="table-wrap" style={{ maxHeight: 420 }}>
            <table>
              <thead>
                <tr>
                  <th>Contract</th>
                  <th>Qty</th>
                  <th>Mult.</th>
                  <th>Market price</th>
                  <th>Model price</th>
                  <th>Used</th>
                  <th>Method</th>
                  <th>Value</th>
                  <th>Delta</th>
                  <th>IV</th>
                  <th>Greeks from</th>
                  <th>Notes</th>
                </tr>
              </thead>
              <tbody>
                {detail.data.results.positions_detail.map((row) => (
                  <tr key={row.position_id}>
                    <td className="mono">{row.canonical_key}</td>
                    <td>{row.quantity}</td>
                    <td>{row.multiplier}</td>
                    <td>{row.market_price ?? "—"}</td>
                    <td className="muted">{row.model_price ?? "—"}</td>
                    <td>{row.price_used ?? "—"}</td>
                    <td>
                      <MethodTag method={row.valuation_method} />
                    </td>
                    <td>{money(row.base_market_value)}</td>
                    <td>{row.greeks.delta.toFixed(2)}</td>
                    <td>
                      {row.implied_volatility === null
                        ? "—"
                        : `${(row.implied_volatility * 100).toFixed(2)}%`}
                    </td>
                    <td className="muted">{row.greek_source}</td>
                    <td className="muted mono">{row.warnings.join(", ") || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Positions</h3>
        <p>
          <Link href={`/portfolios/${portfolioId}/import`}>Import from a file →</Link>
          {"  "}
          <Link href={`/portfolios/${portfolioId}/risk`}>Risk and stress →</Link>
          {"  "}
          <Link href={`/portfolios/${portfolioId}/margin`}>Margin →</Link>
        </p>
        <div className="table-wrap" style={{ maxHeight: 320 }}>
          <table>
            <thead>
              <tr>
                <th>Instrument</th>
                <th>Quantity</th>
                <th>Side</th>
                <th>Average price</th>
                <th>Source</th>
                <th>Tag</th>
              </tr>
            </thead>
            <tbody>
              {(positions.data ?? []).map((position) => (
                <tr key={position.id}>
                  <td className="mono">{position.instrument_id.slice(0, 8)}…</td>
                  <td>{position.quantity}</td>
                  <td>{position.side}</td>
                  <td>{position.average_price ?? "—"}</td>
                  <td className="muted">{position.source}</td>
                  <td>{position.strategy_tag ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {positions.data?.length === 0 && (
          <p className="muted">No positions yet.</p>
        )}
      </div>

      <Disclaimer />
    </>
  );
}

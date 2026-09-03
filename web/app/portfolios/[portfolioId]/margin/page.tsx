"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api";
import { BufferCurve } from "@/components/BufferCurve";
import { Disclaimer, ErrorBanner, Metric, SeverityTag, Warnings } from "@/components/Ui";
import type {
  Envelope,
  Job,
  JobResult,
  MarginModelInfo,
  MarginResult,
  Portfolio,
} from "@/lib/types";

function money(value: number | null | undefined) {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString(undefined, { maximumFractionDigits: 0 });
}

export default function MarginPage() {
  const { portfolioId } = useParams<{ portfolioId: string }>();
  const [rate, setRate] = useState("0.065");
  const [settlement, setSettlement] = useState("10:00");
  const [model, setModel] = useState("SimpleRiskMarginModel");
  const [capital, setCapital] = useState("");
  const [coShock, setCoShock] = useState("0");
  const [shortMin, setShortMin] = useState("0");
  const [concentration, setConcentration] = useState("0");
  const [jobId, setJobId] = useState<string | null>(null);

  const portfolio = useQuery({
    queryKey: ["portfolio", portfolioId],
    queryFn: () => api.get<Portfolio>(`/portfolios/${portfolioId}`),
  });

  const models = useQuery({
    queryKey: ["margin-models"],
    queryFn: () => api.get<MarginModelInfo[]>("/margin/models"),
  });

  const run = useMutation({
    mutationFn: async () => {
      const accepted = await api.post<{ job_id: string }>(
        `/portfolios/${portfolioId}/margin`,
        {
          risk_free_rate: rate === "" ? 0 : Number(rate),
          dividend_yield: 0,
          settlement_time_utc: settlement === "" ? null : `${settlement}:00`,
          margin_model: model,
          eligible_capital: capital === "" ? null : Number(capital),
          vol_co_shock: Number(coShock) || 0,
          short_option_minimum_rate: Number(shortMin) || 0,
          concentration_add_on_rate: Number(concentration) || 0,
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
    queryFn: () => api.get<JobResult>(`/jobs/${jobId}/result`),
    enabled: job.data?.status === "COMPLETED",
  });

  const envelope = jobResult.data?.result as Envelope<MarginResult> | undefined;
  const margin = envelope?.results;
  const selected = models.data?.find((item) => item.name === model);

  return (
    <>
      <h2>Margin — {portfolio.data?.name ?? "portfolio"}</h2>
      <p className="subtitle">
        An estimate from a model defined in this repository, under assumptions
        you can read. It is not your broker&rsquo;s margin requirement, which this
        platform does not have and does not model.
      </p>

      <ErrorBanner error={run.error ?? models.error} />

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Model</h3>
        <div className="row">
          <div className="field" style={{ flex: 1 }}>
            <label htmlFor="model">Margin model</label>
            <select
              id="model"
              value={model}
              style={{ width: "100%" }}
              onChange={(event) => setModel(event.target.value)}
            >
              {(models.data ?? []).map((item) => (
                <option key={item.name} value={item.name}>
                  {item.name} v{item.version}
                </option>
              ))}
            </select>
          </div>
        </div>
        {selected && (
          <p className="muted" style={{ marginTop: 0 }}>
            {selected.description}{" "}
            <span className="tag warn">not broker equivalent</span>
          </p>
        )}
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Inputs</h3>
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
          <div className="field">
            <label htmlFor="capital">Eligible capital</label>
            <input
              id="capital"
              placeholder="unknown"
              value={capital}
              onChange={(e) => setCapital(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="coshock">Volatility co-shock</label>
            <input id="coshock" value={coShock} onChange={(e) => setCoShock(e.target.value)} />
          </div>
        </div>
        <p className="muted" style={{ marginTop: 0 }}>
          Leaving capital blank leaves utilisation and buffer undefined rather
          than defaulting them to the portfolio&rsquo;s value, which is a
          different and usually wrong quantity.
        </p>

        <div className="row">
          <div className="field">
            <label htmlFor="shortmin">Short-option minimum rate</label>
            <input id="shortmin" value={shortMin} onChange={(e) => setShortMin(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="conc">Concentration add-on rate</label>
            <input
              id="conc"
              value={concentration}
              onChange={(e) => setConcentration(e.target.value)}
            />
          </div>
          <button
            onClick={() => run.mutate()}
            disabled={run.isPending}
            style={{ alignSelf: "flex-end" }}
          >
            {run.isPending ? "Submitting…" : "Estimate"}
          </button>
        </div>
        <p className="muted" style={{ marginBottom: 0 }}>
          Both rates default to zero on purpose. A short option far out of the
          money shows almost no loss on a scan grid while its tail risk is
          unbounded, and a real margin system floors it for that reason — but the
          rate at which it does is a venue&rsquo;s rule, and picking a number
          here would be inventing one.
        </p>
        {job.data && (
          <p style={{ marginBottom: 0 }}>
            <SeverityTag severity={job.data.status === "FAILED" ? "ERROR" : "INFO"} />{" "}
            {job.data.status}
          </p>
        )}
      </div>

      {envelope?.warnings?.length ? <Warnings warnings={envelope.warnings} /> : null}

      {margin && (
        <>
          <div className="banner warn">{margin.margin.disclaimer}</div>

          <div className="row">
            <Metric
              label="Estimated margin"
              value={money(margin.estimated_margin)}
              unit={`${margin.currency}, under ${margin.method}`}
            />
            <Metric
              label="Buffer"
              value={money(margin.buffer)}
              unit={
                margin.eligible_capital === null
                  ? "no capital supplied"
                  : `capital ${money(margin.eligible_capital)}`
              }
            />
            <Metric
              label="Utilisation"
              value={
                margin.utilisation === null
                  ? "—"
                  : `${(margin.utilisation * 100).toFixed(1)}%`
              }
              unit="estimated margin over stated capital"
            />
            <Metric
              label="Confidence"
              value={margin.margin.confidence.toFixed(2)}
              unit="coverage, grid containment, mark consistency"
            />
          </div>

          <div className="card">
            <h3 style={{ marginTop: 0 }}>What this says</h3>
            <p>{margin.summary}</p>
            {margin.in_shortfall_at_rest && (
              <div className="banner error">
                The estimated buffer is already at or below zero.
              </div>
            )}
          </div>

          {margin.eligible_capital !== null && (
            <div className="card">
              <h3 style={{ marginTop: 0 }}>Estimated buffer across the ladder</h3>
              <BufferCurve
                ladder={margin.ladder}
                downside={margin.shortfall_region.downside}
                upside={margin.shortfall_region.upside}
                currency={margin.currency}
              />
            </div>
          )}

          <div className="card">
            <h3 style={{ marginTop: 0 }}>How the estimate is built</h3>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Component</th>
                    <th>Amount</th>
                    <th>Basis</th>
                  </tr>
                </thead>
                <tbody>
                  {margin.margin.components.map((component) => (
                    <tr key={component.name}>
                      <td className="mono">{component.name}</td>
                      <td>{money(component.amount)}</td>
                      <td className="muted">{component.basis}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="muted" style={{ marginBottom: 0 }}>
              Worst loss of {money(margin.margin.worst_case.loss)} at a{" "}
              {(margin.margin.worst_case.spot_return * 100).toFixed(0)}% move with{" "}
              {(margin.margin.worst_case.vol_points * 100).toFixed(0)} volatility
              points, over a {margin.margin.parameters.grid.points}-point grid.
              {margin.margin.worst_case.at_grid_edge
                ? " That point sits at the edge of the grid, so the true worst case over a wider range is larger."
                : ""}
            </p>
          </div>

          <div className="card">
            <h3 style={{ marginTop: 0 }}>Assumptions</h3>
            <ul className="reasons">
              {margin.margin.assumptions.map((item) => (
                <li key={item}>{item}</li>
              ))}
              {margin.assumptions.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>

          {margin.eligible_capital !== null && (
            <div className="card">
              <h3 style={{ marginTop: 0 }}>The ladder</h3>
              <div className="table-wrap" style={{ maxHeight: 380 }}>
                <table>
                  <thead>
                    <tr>
                      <th>Move</th>
                      <th>Portfolio value</th>
                      <th>Available capital</th>
                      <th>Estimated margin</th>
                      <th>Buffer</th>
                      <th>Utilisation</th>
                    </tr>
                  </thead>
                  <tbody>
                    {margin.ladder.map((point) => (
                      <tr key={point.spot_return}>
                        <td>{(point.spot_return * 100).toFixed(1)}%</td>
                        <td>{money(point.portfolio_value)}</td>
                        <td>{money(point.available_capital)}</td>
                        <td>{money(point.estimated_margin)}</td>
                        <td>
                          {money(point.buffer)}{" "}
                          {point.in_shortfall ? (
                            <span className="tag bad">shortfall</span>
                          ) : null}
                        </td>
                        <td>
                          {point.utilisation === null
                            ? "—"
                            : `${(point.utilisation * 100).toFixed(0)}%`}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}

      <div className="card">
        <p style={{ margin: 0 }}>
          <Link href={`/portfolios/${portfolioId}/risk`}>Value at Risk and stress →</Link>
        </p>
      </div>

      <Disclaimer />
    </>
  );
}

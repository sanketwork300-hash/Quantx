"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import { DispersionBar } from "@/components/DispersionBar";
import { Disclaimer, ErrorBanner, Metric, Warnings } from "@/components/Ui";
import type { Job, JobResult, ModelConsensus } from "@/lib/types";

export default function ConsensusPage() {
  const queryClient = useQueryClient();
  const [instrumentId, setInstrumentId] = useState("");
  const [rate, setRate] = useState("0.065");
  const [dividend, setDividend] = useState("0");
  const [paths, setPaths] = useState("100000");
  const [seed, setSeed] = useState("20260924");
  const [jobId, setJobId] = useState<string | null>(null);

  const runs = useQuery({
    queryKey: ["consensus-runs"],
    queryFn: () => api.get<ModelConsensus[]>("/derivatives/consensus"),
  });

  const price = useMutation({
    mutationFn: async () => {
      const accepted = await api.post<{ job_id: string }>("/derivatives/consensus", {
        instrument_id: instrumentId.trim(),
        risk_free_rate: Number(rate),
        dividend_yield: Number(dividend),
        paths: Number(paths),
        seed: Number(seed),
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
      query.state.data && ["COMPLETED", "FAILED", "CANCELLED"].includes(query.state.data.status)
        ? false
        : 1000,
  });

  const result = useQuery({
    queryKey: ["job-result", jobId],
    queryFn: async () => {
      const body = await api.get<JobResult>(`/jobs/${jobId}/result`);
      queryClient.invalidateQueries({ queryKey: ["consensus-runs"] });
      return body;
    },
    enabled: job.data?.status === "COMPLETED",
  });

  const payload = result.data?.result?.results as
    | (ModelConsensus & {
        counts: { models_requested: number; models_available: number };
        confidence: {
          score: number;
          contributions: { name: string; score: number; weight: number; basis: string }[];
          weakest_contribution: string | null;
        };
        interpretation: string;
        higher_order_greeks: Record<string, number | Record<string, string>> | null;
      })
    | undefined;

  return (
    <>
      <h2>Model consensus</h2>
      <p className="subtitle">
        One contract, priced by every model the platform has. Black-Scholes
        assumes a volatility the smile says does not exist; the local-volatility
        PDE reproduces today&apos;s surface and gets tomorrow&apos;s dynamics
        wrong; Heston has the right kind of dynamics and only approximately the
        right surface; the simulation carries a standard error. There is no
        single number here, and no field that could hold one: where the models
        disagree, the width of the range they span is the most honest thing that
        can be said about the contract.
      </p>

      <ErrorBanner error={price.error} />

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Price a contract</h3>
        <div className="row">
          <div className="field" style={{ minWidth: 340 }}>
            <label htmlFor="instrument">Option instrument id</label>
            <input
              id="instrument"
              value={instrumentId}
              placeholder="uuid of a vanilla option"
              onChange={(event) => setInstrumentId(event.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="rate">Risk-free rate</label>
            <input id="rate" value={rate} onChange={(e) => setRate(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="dividend">Dividend yield</label>
            <input id="dividend" value={dividend} onChange={(e) => setDividend(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="paths">Simulated paths</label>
            <input id="paths" value={paths} onChange={(e) => setPaths(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="seed">Seed</label>
            <input id="seed" value={seed} onChange={(e) => setSeed(e.target.value)} />
          </div>
        </div>
        <p className="muted" style={{ marginTop: 0 }}>
          The seed is recorded on the stored run: the same seed and path count
          reproduce the simulated value exactly, and a different seed will not.
          A global surface must already be calibrated for this underlying — the
          models read their volatility from it rather than from an assumption.
        </p>
        <button
          onClick={() => price.mutate()}
          disabled={!instrumentId.trim() || price.isPending}
        >
          {price.isPending ? "Submitting…" : "Run every model"}
        </button>
      </div>

      {job.data && job.data.status !== "COMPLETED" && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Job</h3>
          <p>{job.data.status}</p>
          <div className="bar">
            <span style={{ width: `${Math.round(job.data.progress * 100)}%` }} />
          </div>
          {job.data.error && (
            <div className="banner error">{String(job.data.error.message ?? "Job failed")}</div>
          )}
        </div>
      )}

      {payload && (
        <>
          <div className="card">
            <h3 style={{ marginTop: 0 }}>Where the models landed</h3>
            <DispersionBar
              values={payload.values}
              referenceValue={payload.reference_value}
              marketPrice={payload.market_price}
            />
            <div className="grid">
              <Metric
                label="Range"
                value={
                  payload.reference_range
                    ? `${payload.reference_range[0].toFixed(4)} … ${payload.reference_range[1].toFixed(4)}`
                    : "—"
                }
              />
              <Metric
                label="Dispersion"
                value={
                  payload.model_dispersion.relative !== null
                    ? `${(payload.model_dispersion.relative * 100).toFixed(2)}%`
                    : "—"
                }
              />
              <Metric
                label="Reference value (median)"
                value={payload.reference_value?.toFixed(4) ?? "—"}
              />
              <Metric
                label="Models"
                value={`${payload.counts.models_available} / ${payload.counts.models_requested}`}
              />
              <Metric label="Confidence" value={payload.confidence.score.toFixed(3)} />
              <Metric
                label="Observed mid"
                value={payload.market_price?.toFixed(4) ?? "not two-sided"}
              />
            </div>
            <p className="muted" style={{ marginBottom: 0 }}>{payload.interpretation}</p>
          </div>

          <div className="card">
            <h3 style={{ marginTop: 0 }}>Each model</h3>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Model</th>
                    <th>Value</th>
                    <th>Method</th>
                    <th>Notes</th>
                  </tr>
                </thead>
                <tbody>
                  {payload.values.map((value) => (
                    <tr key={value.model}>
                      <td>
                        {value.model}
                        <div className="muted" style={{ fontSize: 11 }}>
                          {value.model_version}
                        </div>
                      </td>
                      <td>{value.value?.toFixed(6) ?? "—"}</td>
                      <td style={{ maxWidth: 300 }}>{value.method}</td>
                      <td style={{ maxWidth: 380 }} className="muted">
                        {value.unavailable_reason ?? value.warnings.join(" ")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="card">
            <h3 style={{ marginTop: 0 }}>Why the confidence is what it is</h3>
            <ul className="reasons">
              {payload.confidence.contributions.map((contribution) => (
                <li key={contribution.name}>
                  <span className="mono">{contribution.name}</span>{" "}
                  {contribution.score.toFixed(3)} (weight {contribution.weight}) —{" "}
                  {contribution.basis}
                </li>
              ))}
            </ul>
            {payload.confidence.weakest_contribution && (
              <p className="muted" style={{ marginBottom: 0 }}>
                The weakest dimension is{" "}
                <span className="mono">{payload.confidence.weakest_contribution}</span>.
                The score is a weighted geometric mean, so one bad dimension
                pulls the whole thing down rather than being averaged away.
              </p>
            )}
          </div>

          {payload.higher_order_greeks && (
            <div className="card">
              <h3 style={{ marginTop: 0 }}>Higher-order Greeks</h3>
              <div className="grid">
                <Metric
                  label="Vanna (per vol point)"
                  value={Number(payload.higher_order_greeks.vanna_per_vol_point).toFixed(6)}
                />
                <Metric
                  label="Volga (per vol point)"
                  value={Number(payload.higher_order_greeks.volga_per_vol_point).toFixed(6)}
                />
                <Metric
                  label="Charm (per day)"
                  value={Number(payload.higher_order_greeks.charm_per_day).toFixed(6)}
                />
              </div>
            </div>
          )}

          <Warnings warnings={result.data?.result?.warnings ?? []} />
        </>
      )}

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Previous runs</h3>
        <ErrorBanner error={runs.error} />
        <div className="table-wrap" style={{ maxHeight: 320 }}>
          <table>
            <thead>
              <tr>
                <th>Run</th>
                <th>Contract</th>
                <th>Reference value</th>
                <th>Range</th>
                <th>Dispersion</th>
                <th>Models</th>
                <th>Confidence</th>
              </tr>
            </thead>
            <tbody>
              {(runs.data ?? []).map((row) => (
                <tr key={row.consensus_row_id}>
                  <td>{new Date(row.created_at).toLocaleString()}</td>
                  <td>
                    {row.strike} {row.option_type} {row.expiry}
                  </td>
                  <td>{row.reference_value?.toFixed(4) ?? "—"}</td>
                  <td className="mono">
                    {row.reference_range
                      ? `${row.reference_range[0].toFixed(3)} … ${row.reference_range[1].toFixed(3)}`
                      : "—"}
                  </td>
                  <td>
                    {row.model_dispersion.relative !== null
                      ? `${(row.model_dispersion.relative * 100).toFixed(2)}%`
                      : "—"}
                  </td>
                  <td>
                    {row.models_available} / {row.models_requested}
                  </td>
                  <td>{row.confidence.toFixed(3)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <Disclaimer />
    </>
  );
}

"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api";
import { SurfaceChart } from "@/components/SurfaceChart";
import {
  Disclaimer,
  ErrorBanner,
  Metric,
  ScoreTag,
  SeverityTag,
  Warnings,
} from "@/components/Ui";
import type {
  ArbitrageReport,
  ArbitrageResults,
  ChainAnalysis,
  Envelope,
  Job,
  JobResult,
  Surface,
  SurfaceSlice,
} from "@/lib/types";

function pct(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(digits)}%`;
}

function ScopePanel({
  title,
  meaning,
  report,
}: {
  title: string;
  meaning: string;
  report: ArbitrageReport | null;
}) {
  if (!report) return null;
  const clean = report.violations_total === 0;
  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>
        {title}{" "}
        {clean ? (
          <span className="tag good">no violations</span>
        ) : (
          <span className={`tag ${report.severity === "ERROR" ? "bad" : "warn"}`}>
            {report.violations_total} violation{report.violations_total === 1 ? "" : "s"}
          </span>
        )}
      </h3>
      <p className="muted" style={{ marginTop: 0 }}>
        {meaning}
      </p>
      <p className="muted" style={{ fontSize: 11 }}>
        Checks run: {report.checks_run.join(", ")} · {report.observations} observation
        {report.observations === 1 ? "" : "s"}
      </p>

      {!clean && (
        <div className="table-wrap" style={{ maxHeight: 320 }}>
          <table>
            <thead>
              <tr>
                <th>Condition</th>
                <th>Severity</th>
                <th>Expiry</th>
                <th>Strike</th>
                <th>Magnitude</th>
                <th>Tolerance</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {report.violations.map((violation, index) => (
                <tr key={index}>
                  <td className="mono">{violation.type}</td>
                  <td>
                    <SeverityTag severity={violation.severity} />
                  </td>
                  <td>{violation.expiry ?? "—"}</td>
                  <td>
                    {violation.strike ?? "—"}
                    {violation.option_type ? ` ${violation.option_type[0]}` : ""}
                  </td>
                  <td>{violation.magnitude.toPrecision(4)}</td>
                  <td>{violation.tolerance?.toPrecision(3) ?? "—"}</td>
                  <td className="muted" style={{ fontSize: 11, whiteSpace: "normal" }}>
                    {String(
                      violation.detail.condition ??
                        violation.detail.bound ??
                        violation.detail.meaning ??
                        violation.detail.note ??
                        "",
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function SlicePanel({ slice }: { slice: SurfaceSlice }) {
  const metrics = slice.calibration;
  const params = slice.parameters;
  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>
        {slice.expiry}{" "}
        <span className={`tag ${metrics.status === "CONVERGED" ? "good" : "warn"}`}>
          {metrics.status}
        </span>
      </h3>

      {!params ? (
        <div className="banner warn">Not fitted: {metrics.error ?? "no parameters"}</div>
      ) : (
        <>
          <div className="grid">
            <Metric label="RMSE" value={metrics.rmse_vol_points?.toFixed(4) ?? "—"} unit="volatility points" />
            <Metric label="Worst point" value={metrics.max_error_vol_points?.toFixed(4) ?? "—"} unit="volatility points" />
            <Metric label="Quotes used" value={metrics.n_observations} unit={`k ∈ [${slice.k_min?.toFixed(3)}, ${slice.k_max?.toFixed(3)}]`} />
            <Metric label="Forward" value={slice.forward.toFixed(2)} unit={slice.forward_method ?? ""} />
          </div>

          <h3>Parameters (raw SVI)</h3>
          <p className="muted" style={{ marginTop: 0, fontSize: 11 }}>
            w(k) = a + b[ρ(k − m) + √((k − m)² + σ²)]. These five numbers, the
            forward and the maturity are all a reference value depends on — the
            surface is never re-fitted on read.
          </p>
          <table>
            <tbody>
              <tr>
                <td>a (level)</td>
                <td className="mono">{params.a.toPrecision(8)}</td>
                <td>b (slope)</td>
                <td className="mono">{params.b.toPrecision(8)}</td>
              </tr>
              <tr>
                <td>ρ (skew)</td>
                <td className="mono">{params.rho.toPrecision(8)}</td>
                <td>m (shift)</td>
                <td className="mono">{params.m.toPrecision(8)}</td>
              </tr>
              <tr>
                <td>σ (curvature)</td>
                <td className="mono">{params.sigma.toPrecision(8)}</td>
                <td />
                <td />
              </tr>
            </tbody>
          </table>

          <h3>Admissibility</h3>
          <table>
            <tbody>
              <tr>
                <td>
                  Durrleman min g
                  <div className="muted" style={{ fontSize: 11 }}>
                    negative would mean a negative implied density
                  </div>
                </td>
                <td>
                  {metrics.min_durrleman_g?.toPrecision(4) ?? "—"}{" "}
                  {metrics.min_durrleman_g !== null && metrics.min_durrleman_g > 0 ? (
                    <span className="tag good">butterfly free</span>
                  ) : (
                    <span className="tag bad">violated</span>
                  )}
                </td>
              </tr>
              <tr>
                <td>
                  Wing slope b(1+|ρ|)
                  <div className="muted" style={{ fontSize: 11 }}>
                    Lee&apos;s moment formula bounds this at 2
                  </div>
                </td>
                <td>
                  {metrics.wing_slope?.toFixed(4) ?? "—"}{" "}
                  {(metrics.wing_slope ?? 0) <= 2 ? (
                    <span className="tag good">within bound</span>
                  ) : (
                    <span className="tag bad">exceeded</span>
                  )}
                </td>
              </tr>
              <tr>
                <td>Optimizer</td>
                <td className="mono">
                  {metrics.optimizer} · {metrics.iterations} iterations ·{" "}
                  {metrics.starts_feasible}/{metrics.starts_attempted} feasible starts
                </td>
              </tr>
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

export default function SurfacePage() {
  const params = useParams<{ snapshotId: string }>();
  const [jobId, setJobId] = useState<string | null>(null);

  const smile = useQuery({
    queryKey: ["smile-for-surface", params.snapshotId],
    queryFn: () =>
      api.get<Envelope<ChainAnalysis>>(
        `/derivatives/chains/${params.snapshotId}/smile?used_for_smile_only=false`,
      ),
    retry: false,
  });
  const analysisId = smile.data?.results?.analysis_id;

  const surfaces = useQuery({
    queryKey: ["surfaces"],
    queryFn: () => api.get<{ surface_row_id: string; analysis_id: string }[]>("/derivatives/surfaces"),
    retry: false,
  });
  const surfaceRowId = surfaces.data?.find((s) => s.analysis_id === analysisId)?.surface_row_id;

  const surface = useQuery({
    queryKey: ["surface", surfaceRowId],
    queryFn: () => api.get<Envelope<Surface>>(`/derivatives/surfaces/${surfaceRowId}`),
    enabled: Boolean(surfaceRowId),
  });

  const arbitrage = useQuery({
    queryKey: ["arbitrage", analysisId],
    queryFn: () => api.get<Envelope<ArbitrageResults>>(`/derivatives/arbitrage/${analysisId}`),
    enabled: Boolean(analysisId),
  });

  const run = useMutation({
    mutationFn: async () => {
      if (!analysisId) throw new Error("Run the implied-volatility analysis first.");
      const accepted = await api.post<{ job_id: string }>(
        `/derivatives/analyses/${analysisId}/calibrate`,
        { seed: 20260924, use_weights: true },
      );
      return accepted.job_id;
    },
    onSuccess: (id) => setJobId(id),
  });

  const job = useQuery({
    queryKey: ["calibrate-job", jobId],
    queryFn: () => api.get<Job>(`/jobs/${jobId}`),
    enabled: jobId !== null,
    refetchInterval: (query) =>
      query.state.data && ["COMPLETED", "FAILED", "CANCELLED"].includes(query.state.data.status)
        ? false
        : 800,
  });

  const jobResult = useQuery({
    queryKey: ["calibrate-job-result", jobId],
    queryFn: async () => {
      const payload = await api.get<JobResult>(`/jobs/${jobId}/result`);
      await surfaces.refetch();
      await surface.refetch();
      await arbitrage.refetch();
      return payload;
    },
    enabled: job.data?.status === "COMPLETED",
  });

  const fitted = surface.data?.results;
  const observed = smile.data?.results?.slices ?? [];
  const reports = arbitrage.data?.results;

  return (
    <>
      <h2>Volatility surface</h2>
      <p className="subtitle">
        Raw SVI fitted per expiry, with the no-arbitrage conditions imposed as
        constraints rather than checked afterwards. Everything here is a model
        output: reference values, never fair values.
      </p>

      <p>
        <Link href={`/markets/chains/${params.snapshotId}/smile`}>← implied volatility</Link>
        {"  ·  "}
        <Link href={`/markets/chains/${params.snapshotId}/scanner`}>
          scan for deviations →
        </Link>
      </p>

      <ErrorBanner error={run.error} />

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Calibrate</h3>
        <div className="row">
          <button onClick={() => run.mutate()} disabled={!analysisId || run.isPending}>
            {run.isPending ? "Submitting…" : "Fit SVI"}
          </button>
          {job.data && (
            <span>
              <SeverityTag severity={job.data.status === "FAILED" ? "ERROR" : "INFO"} /> job{" "}
              {job.data.status}
            </span>
          )}
        </div>
        <p className="muted" style={{ marginBottom: 0 }}>
          Calibration is deterministic: the same analysis and seed refit to the
          same parameters, which is what lets a stored surface be reproduced
          rather than merely re-run. Quotes are weighted by the spread and
          liquidity scores from the quality engine.
        </p>
      </div>

      {jobResult.data?.result && <Warnings warnings={jobResult.data.result.warnings} />}

      {fitted && (
        <>
          <div className="grid">
            <Metric label="Surface" value={<span className="mono" style={{ fontSize: 12 }}>{fitted.surface_id}</span>} unit="content-addressed" />
            <Metric label="Slices fitted" value={`${fitted.counts.fitted} / ${fitted.counts.slices}`} />
            <Metric label="Model" value={fitted.model} unit={fitted.model_version} />
            <Metric
              label="Worst slice RMSE"
              value={
                Math.max(
                  ...fitted.slices.map((s) => s.calibration.rmse_vol_points ?? 0),
                ).toFixed(4)
              }
              unit="volatility points"
            />
          </div>

          <div className="card">
            <h3 style={{ marginTop: 0 }}>Observed and fitted</h3>
            <SurfaceChart observed={observed} fitted={fitted.slices} />
          </div>

          {reports && (
            <>
              <h3>Arbitrage diagnostics</h3>
              <p className="muted" style={{ marginTop: 0 }}>
                Reported in two scopes and never merged. A smooth fit must not be
                able to hide a broken market, and a broken fit must not be
                reported as a market anomaly.
              </p>
              <ScopePanel
                title="Observed market"
                meaning="A violation here is almost always a data artefact — stale legs, non-simultaneous quotes, a wrong multiplier — not an executable opportunity."
                report={reports.raw_market}
              />
              <ScopePanel
                title="Fitted surface"
                meaning="A violation here is a defect in our model, not in the market. Per-expiry SVI cannot prevent calendar arbitrage across slices; it is detected and reported until SSVI arrives."
                report={reports.fitted_surface}
              />
            </>
          )}

          <h3>Slices</h3>
          {fitted.slices.map((slice) => (
            <SlicePanel key={slice.expiry} slice={slice} />
          ))}
        </>
      )}

      {!fitted && !surface.isLoading && (
        <div className="banner note">
          No surface for this chain yet.{" "}
          {analysisId
            ? "Fit one above."
            : "Run the implied-volatility analysis first."}
        </div>
      )}

      <Disclaimer />
    </>
  );
}

"use client";

import { useQuery } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import { api } from "@/lib/api";
import { Disclaimer, ErrorBanner, Metric } from "@/components/Ui";
import type {
  Envelope,
  GlobalSurface,
  HestonCalibration,
  LocalVolatilitySurface,
  RiskNeutralDensity,
} from "@/lib/types";

const PERCENTILES = ["0.05", "0.25", "0.5", "0.75", "0.95"];

function heat(value: number | null, min: number, max: number): string {
  if (value === null) return "repeating-linear-gradient(45deg,#1c2128,#1c2128 4px,#262c36 4px,#262c36 8px)";
  const t = max > min ? (value - min) / (max - min) : 0.5;
  return `hsl(${(1 - t) * 210}, 62%, ${28 + t * 26}%)`;
}

export default function GlobalSurfacePage() {
  const params = useParams<{ surfaceId: string }>();
  const id = params.surfaceId;

  const surface = useQuery({
    queryKey: ["global-surface", id],
    queryFn: () => api.get<Envelope<GlobalSurface>>(`/derivatives/global-surfaces/${id}`),
  });
  const localVol = useQuery({
    queryKey: ["local-vol", id],
    queryFn: () =>
      api.get<LocalVolatilitySurface>(
        `/derivatives/global-surfaces/${id}/local-volatility`,
      ),
    retry: false,
  });
  const densities = useQuery({
    queryKey: ["densities", id],
    queryFn: () =>
      api.get<RiskNeutralDensity[]>(`/derivatives/global-surfaces/${id}/densities`),
    retry: false,
  });
  const heston = useQuery({
    queryKey: ["heston", surface.data?.results?.underlying_id],
    queryFn: () =>
      api.get<HestonCalibration>(
        `/derivatives/underlyings/${surface.data?.results?.underlying_id}/heston`,
      ),
    enabled: Boolean(surface.data?.results?.underlying_id),
    retry: false,
  });

  const results = surface.data?.results;
  const grid = localVol.data;
  const flat = (grid?.values ?? []).flat().filter((v): v is number => v !== null);
  const min = flat.length ? Math.min(...flat) : 0;
  const max = flat.length ? Math.max(...flat) : 1;

  return (
    <>
      <h2>Global surface</h2>
      <ErrorBanner error={surface.error} />

      {results && (
        <>
          <div className="card">
            <h3 style={{ marginTop: 0 }}>SSVI</h3>
            <div className="grid">
              <Metric label="rho" value={results.parameters?.rho.toFixed(4) ?? "—"} />
              <Metric label="eta" value={results.parameters?.eta.toFixed(4) ?? "—"} />
              <Metric label="gamma" value={results.parameters?.gamma.toFixed(4) ?? "—"} />
              <Metric label="Status" value={results.status} />
              <Metric
                label="RMSE (vol pts)"
                value={results.diagnostics.rmse_vol_points?.toFixed(3) ?? "—"}
              />
              <Metric
                label="min Durrleman g"
                value={results.diagnostics.min_durrleman_g?.toExponential(2) ?? "—"}
              />
            </div>
            <p className="muted" style={{ marginBottom: 0 }}>
              {results.diagnostics.calendar_arbitrage_free
                ? "The at-the-money variance term structure is non-decreasing, which for SSVI is the no-calendar-arbitrage condition itself rather than a check that happened to pass."
                : "The fitted variance term structure is not non-decreasing. Reference values from this surface are usable with care and carry the condition they fail."}
              {results.diagnostics.butterfly_bounds_satisfied
                ? " The closed-form butterfly bounds of Gatheral-Jacquier Theorem 4.2 hold as well."
                : " The closed-form butterfly bounds do not hold. They are sufficient, not necessary, so this is a surface the theorem cannot certify rather than one with a negative density in it — Durrleman's condition above is the one that decides."}
            </p>
          </div>

          <div className="card">
            <h3 style={{ marginTop: 0 }}>At-the-money variance term structure</h3>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Expiry</th>
                    <th>tau</th>
                    <th>theta</th>
                    <th>ATM vol</th>
                    <th>Forward</th>
                    <th>Quotes</th>
                    <th>RMSE (vol pts)</th>
                    <th>Fitted k range</th>
                  </tr>
                </thead>
                <tbody>
                  {results.slices.map((slice) => (
                    <tr key={slice.expiry}>
                      <td>{slice.expiry}</td>
                      <td>{slice.time_to_expiry.toFixed(4)}</td>
                      <td className="mono">{slice.theta.toExponential(4)}</td>
                      <td>{slice.atm_volatility?.toFixed(4) ?? "—"}</td>
                      <td>{slice.forward.toFixed(2)}</td>
                      <td>{slice.diagnostics?.n_observations ?? "—"}</td>
                      <td>{slice.diagnostics?.rmse_vol_points.toFixed(3) ?? "—"}</td>
                      <td className="mono">
                        {slice.diagnostics
                          ? `${slice.diagnostics.k_min.toFixed(3)} … ${slice.diagnostics.k_max.toFixed(3)}`
                          : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}

      {grid && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Dupire local volatility</h3>
          <p className="muted" style={{ marginTop: 0 }}>
            {grid.valid_points} of {grid.total_points} grid points carry a value.
            The hatched cells are where Dupire&apos;s denominator vanishes and
            the formula said nothing; they are holes with reasons rather than an
            interpolation over a region the surface never described.
          </p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>tau \ k</th>
                  {grid.log_moneyness
                    .filter((_, index) => index % 4 === 0)
                    .map((k) => (
                      <th key={k}>{k.toFixed(2)}</th>
                    ))}
                </tr>
              </thead>
              <tbody>
                {grid.maturities.map((tau, row) => (
                  <tr key={tau}>
                    <td>{tau.toFixed(3)}</td>
                    {grid.values[row]
                      .filter((_, index) => index % 4 === 0)
                      .map((value, index) => (
                        <td
                          key={index}
                          title={value === null ? "no value: Dupire's denominator vanishes here" : value.toFixed(4)}
                          style={{ background: heat(value, min, max), textAlign: "center" }}
                        >
                          {value === null ? "" : value.toFixed(3)}
                        </td>
                      ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {Object.keys(grid.flag_counts).length > 0 && (
            <ul className="reasons">
              {Object.entries(grid.flag_counts).map(([flag, count]) => (
                <li key={flag}>
                  <span className="mono">{flag}</span> — {count}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {densities.data && densities.data.length > 0 && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Risk-neutral density</h3>
          <p className="muted" style={{ marginTop: 0 }}>
            The distribution the option market is pricing under, not a forecast
            of where the underlying will go. Quantiles are shown only where the
            density is non-negative and integrates to one; otherwise the strike
            range does not contain the distribution and a quantile read off it
            would be a quantile of the window.
          </p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Expiry</th>
                  <th>Mass</th>
                  <th>Mean vs forward</th>
                  <th>Admissible</th>
                  {PERCENTILES.map((p) => (
                    <th key={p}>p{Math.round(Number(p) * 100)}</th>
                  ))}
                  <th>Flags</th>
                </tr>
              </thead>
              <tbody>
                {densities.data.map((density) => (
                  <tr key={density.density_row_id}>
                    <td>{density.expiry}</td>
                    <td>{density.total_mass.toFixed(4)}</td>
                    <td>{(density.mean_error * 10000).toFixed(1)} bp</td>
                    <td>{density.is_admissible ? "yes" : "no"}</td>
                    {PERCENTILES.map((p) => (
                      <td key={p}>{density.percentiles[p]?.toFixed(2) ?? "—"}</td>
                    ))}
                    <td className="mono" style={{ fontSize: 11 }}>
                      {density.flags.join(", ") || "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {heston.data && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Heston</h3>
          <div className="grid">
            <Metric label="v0" value={heston.data.v0?.toFixed(4) ?? "—"} />
            <Metric label="kappa" value={heston.data.kappa?.toFixed(4) ?? "—"} />
            <Metric label="theta" value={heston.data.theta?.toFixed(4) ?? "—"} />
            <Metric label="xi" value={heston.data.xi?.toFixed(4) ?? "—"} />
            <Metric label="rho" value={heston.data.rho?.toFixed(4) ?? "—"} />
            <Metric
              label="RMSE (vol pts)"
              value={heston.data.rmse_vol_points?.toFixed(3) ?? "—"}
            />
          </div>
          <p className="muted" style={{ marginBottom: 0 }}>
            Feller <span className="mono">2·kappa·theta − xi²</span> ={" "}
            {heston.data.feller?.toFixed(4) ?? "—"}.{" "}
            {heston.data.satisfies_feller
              ? "The variance process stays strictly positive."
              : "The variance process can reach zero. This is reported and not enforced: real index surfaces routinely calibrate to parameter sets that violate the condition, and refusing them would mean refusing to describe the market."}
          </p>
        </div>
      )}

      <Disclaimer />
    </>
  );
}

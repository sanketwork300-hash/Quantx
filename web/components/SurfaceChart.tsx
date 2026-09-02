"use client";

import { useMemo, useState } from "react";
import type { ImpliedVolPoint, SmileSlice, SurfaceSlice } from "@/lib/types";

const SERIES_COLOURS = ["#4c9aff", "#3fb950", "#d29922", "#f778ba", "#a371f7"];

type Axis = "iv" | "variance";

interface Props {
  observed: SmileSlice[];
  fitted: SurfaceSlice[];
}

/** Raw SVI, evaluated in the browser so the fitted curve is drawn from the
 *  same five numbers the server persisted rather than from a sampled copy. */
function totalVariance(k: number, p: { a: number; b: number; rho: number; m: number; sigma: number }) {
  const shifted = k - p.m;
  return p.a + p.b * (p.rho * shifted + Math.sqrt(shifted * shifted + p.sigma * p.sigma));
}

function niceTicks(min: number, max: number, count = 5): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max) || min === max) return [min];
  const raw = (max - min) / count;
  const magnitude = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) ?? magnitude;
  const ticks: number[] = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-12; v += step) ticks.push(v);
  return ticks;
}

/**
 * Observed quotes and the fitted slice on one pair of axes.
 *
 * The observed points are drawn as markers and the fit as a continuous curve,
 * so the two can never be mistaken for each other — which is the visual form of
 * the rule that observations and model estimates stay separate. The shaded band
 * inside the fitted range marks where the fit is supported by data; outside it
 * the curve is dashed, because SVI's wings are weakly constrained.
 */
export function SurfaceChart({ observed, fitted }: Props) {
  const [axis, setAxis] = useState<Axis>("iv");
  const [hidden, setHidden] = useState<Set<string>>(new Set());

  const width = 800;
  const height = 400;
  const margin = { top: 16, right: 20, bottom: 46, left: 66 };
  const innerW = width - margin.left - margin.right;
  const innerH = height - margin.top - margin.bottom;

  const series = useMemo(() => {
    return observed
      .filter((slice) => !hidden.has(slice.expiry))
      .map((slice, index) => {
        const fit = fitted.find((f) => f.expiry === slice.expiry);
        const points: ImpliedVolPoint[] = slice.points
          .filter((p) => p.market_iv !== null && p.log_moneyness !== null && p.used_for_smile)
          .sort((a, b) => (a.log_moneyness ?? 0) - (b.log_moneyness ?? 0));
        return {
          expiry: slice.expiry,
          colour: SERIES_COLOURS[index % SERIES_COLOURS.length],
          points,
          fit,
          tau: slice.time_to_expiry ?? fit?.time_to_expiry ?? null,
        };
      })
      .filter((s) => s.points.length > 0);
  }, [observed, fitted, hidden]);

  const domain = useMemo(() => {
    const xs: number[] = [];
    const ys: number[] = [];
    for (const s of series) {
      for (const p of s.points) {
        xs.push(p.log_moneyness ?? 0);
        ys.push(axis === "iv" ? p.market_iv ?? 0 : p.total_variance ?? 0);
      }
      if (s.fit?.parameters && s.tau) {
        for (const k of [s.fit.k_min ?? -0.1, s.fit.k_max ?? 0.1]) {
          const w = totalVariance(k, s.fit.parameters);
          ys.push(axis === "iv" ? Math.sqrt(Math.max(w, 0) / s.tau) : w);
        }
      }
    }
    if (!xs.length) return null;
    const padX = (Math.max(...xs) - Math.min(...xs)) * 0.12 || 0.02;
    const padY = (Math.max(...ys) - Math.min(...ys)) * 0.15 || 0.01;
    return {
      x0: Math.min(...xs) - padX,
      x1: Math.max(...xs) + padX,
      y0: Math.max(0, Math.min(...ys) - padY),
      y1: Math.max(...ys) + padY,
    };
  }, [series, axis]);

  if (!domain) {
    return <div className="banner note">Nothing to plot yet.</div>;
  }

  const sx = (k: number) =>
    margin.left + ((k - domain.x0) / (domain.x1 - domain.x0 || 1)) * innerW;
  const sy = (v: number) =>
    margin.top + innerH - ((v - domain.y0) / (domain.y1 - domain.y0 || 1)) * innerH;

  function fitPath(s: (typeof series)[number], from: number, to: number): string {
    if (!s.fit?.parameters || !s.tau) return "";
    const steps = 80;
    const points: string[] = [];
    for (let i = 0; i <= steps; i += 1) {
      const k = from + ((to - from) * i) / steps;
      const w = totalVariance(k, s.fit.parameters);
      const value = axis === "iv" ? Math.sqrt(Math.max(w, 0) / s.tau) : w;
      points.push(`${sx(k)},${sy(value)}`);
    }
    return points.join(" ");
  }

  return (
    <div>
      <div className="row" style={{ marginBottom: 10 }}>
        <button className={axis === "iv" ? "" : "secondary"} onClick={() => setAxis("iv")}>
          Implied volatility
        </button>
        <button
          className={axis === "variance" ? "" : "secondary"}
          onClick={() => setAxis("variance")}
        >
          Total variance
        </button>
        {observed.map((slice, index) => (
          <button
            key={slice.expiry}
            className="secondary"
            style={{
              opacity: hidden.has(slice.expiry) ? 0.4 : 1,
              borderColor: SERIES_COLOURS[index % SERIES_COLOURS.length],
            }}
            onClick={() => {
              const next = new Set(hidden);
              if (next.has(slice.expiry)) next.delete(slice.expiry);
              else next.add(slice.expiry);
              setHidden(next);
            }}
          >
            <span
              style={{
                display: "inline-block",
                width: 8,
                height: 8,
                borderRadius: 8,
                background: SERIES_COLOURS[index % SERIES_COLOURS.length],
                marginRight: 6,
              }}
            />
            {slice.expiry}
          </button>
        ))}
      </div>

      <svg width={width} height={height} role="img" aria-label="Volatility surface">
        {niceTicks(domain.y0, domain.y1).map((tick) => (
          <g key={`y${tick}`}>
            <line
              x1={margin.left}
              x2={width - margin.right}
              y1={sy(tick)}
              y2={sy(tick)}
              stroke="var(--border)"
            />
            <text x={margin.left - 8} y={sy(tick) + 4} textAnchor="end" fontSize="11" fill="var(--muted)">
              {axis === "iv" ? `${(tick * 100).toFixed(1)}%` : tick.toFixed(4)}
            </text>
          </g>
        ))}
        {niceTicks(domain.x0, domain.x1).map((tick) => (
          <g key={`x${tick}`}>
            <line
              y1={margin.top}
              y2={margin.top + innerH}
              x1={sx(tick)}
              x2={sx(tick)}
              stroke="var(--border)"
              strokeDasharray={Math.abs(tick) < 1e-9 ? "" : "2 3"}
            />
            <text x={sx(tick)} y={height - margin.bottom + 16} textAnchor="middle" fontSize="11" fill="var(--muted)">
              {tick.toFixed(2)}
            </text>
          </g>
        ))}

        <text x={margin.left + innerW / 2} y={height - 6} textAnchor="middle" fontSize="11" fill="var(--muted)">
          log-moneyness k = ln(K / F)
        </text>
        <text
          x={14}
          y={margin.top + innerH / 2}
          textAnchor="middle"
          fontSize="11"
          fill="var(--muted)"
          transform={`rotate(-90 14 ${margin.top + innerH / 2})`}
        >
          {axis === "iv" ? "Implied volatility" : "Total variance  w = σ²T"}
        </text>

        {series.map((s) => {
          const kMin = s.fit?.k_min ?? domain.x0;
          const kMax = s.fit?.k_max ?? domain.x1;
          return (
            <g key={s.expiry}>
              {s.fit?.parameters && (
                <>
                  {/* Supported by data */}
                  <polyline
                    points={fitPath(s, kMin, kMax)}
                    fill="none"
                    stroke={s.colour}
                    strokeWidth={2}
                  />
                  {/* Extrapolation: dashed, because SVI's wings are weakly constrained */}
                  {domain.x0 < kMin && (
                    <polyline
                      points={fitPath(s, domain.x0, kMin)}
                      fill="none"
                      stroke={s.colour}
                      strokeWidth={1.5}
                      strokeDasharray="4 4"
                      opacity={0.6}
                    />
                  )}
                  {domain.x1 > kMax && (
                    <polyline
                      points={fitPath(s, kMax, domain.x1)}
                      fill="none"
                      stroke={s.colour}
                      strokeWidth={1.5}
                      strokeDasharray="4 4"
                      opacity={0.6}
                    />
                  )}
                </>
              )}
              {s.points.map((p, index) => (
                <circle
                  key={`${p.instrument_id}-${index}`}
                  cx={sx(p.log_moneyness ?? 0)}
                  cy={sy(axis === "iv" ? p.market_iv ?? 0 : p.total_variance ?? 0)}
                  r={3}
                  fill="none"
                  stroke={s.colour}
                  strokeWidth={1.5}
                >
                  <title>
                    {`observed  K=${p.strike} ${p.option_type}\nmarket IV ${((p.market_iv ?? 0) * 100).toFixed(3)}%\nk=${(p.log_moneyness ?? 0).toFixed(4)}`}
                  </title>
                </circle>
              ))}
            </g>
          );
        })}
      </svg>

      <p className="muted" style={{ fontSize: 11, marginTop: 4 }}>
        Hollow markers are <strong>observed</strong> market implied volatilities.
        Solid lines are the <strong>fitted</strong> SVI slice, drawn in the browser
        from the five persisted parameters. Dashed segments are extrapolation
        beyond the strikes that were fitted, where SVI&apos;s wings are weakly
        constrained — a reference value there carries an EXTRAPOLATED_STRIKE flag.
      </p>
    </div>
  );
}

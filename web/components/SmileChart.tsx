"use client";

import { useMemo, useState } from "react";
import type { SmileSlice } from "@/lib/types";

const SERIES_COLOURS = ["#4c9aff", "#3fb950", "#d29922", "#f778ba", "#a371f7"];

type Axis = "iv" | "variance";

interface Props {
  slices: SmileSlice[];
  onSelectPoint?: (instrumentId: string) => void;
}

function niceTicks(min: number, max: number, count = 5): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max) || min === max) return [min];
  const span = max - min;
  const raw = span / count;
  const magnitude = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) ?? magnitude;
  const start = Math.ceil(min / step) * step;
  const ticks: number[] = [];
  for (let value = start; value <= max + 1e-12; value += step) ticks.push(value);
  return ticks;
}

/**
 * Observed smile, drawn as inline SVG. Deliberately not a charting library: the
 * bid/ask envelope has to be a first-class visual element rather than an
 * afterthought, because it is what tells a reader how much of an apparent
 * deviation is simply the spread.
 */
export function SmileChart({ slices, onSelectPoint }: Props) {
  const [axis, setAxis] = useState<Axis>("iv");
  const [hidden, setHidden] = useState<Set<string>>(new Set());

  const width = 760;
  const height = 380;
  const margin = { top: 16, right: 20, bottom: 44, left: 62 };
  const innerW = width - margin.left - margin.right;
  const innerH = height - margin.top - margin.bottom;

  const series = useMemo(
    () =>
      slices
        .filter((slice) => !hidden.has(slice.expiry))
        .map((slice, index) => ({
          expiry: slice.expiry,
          colour: SERIES_COLOURS[index % SERIES_COLOURS.length],
          points: slice.points
            .filter(
              (p) =>
                p.market_iv !== null &&
                p.log_moneyness !== null &&
                p.used_for_smile,
            )
            .sort((a, b) => (a.log_moneyness ?? 0) - (b.log_moneyness ?? 0)),
        }))
        .filter((s) => s.points.length > 0),
    [slices, hidden],
  );

  const domain = useMemo(() => {
    const xs: number[] = [];
    const ys: number[] = [];
    for (const s of series)
      for (const p of s.points) {
        xs.push(p.log_moneyness ?? 0);
        const centre = axis === "iv" ? p.market_iv ?? 0 : p.total_variance ?? 0;
        ys.push(centre);
        if (axis === "iv" && p.market_iv_bid !== null) ys.push(p.market_iv_bid);
        if (axis === "iv" && p.market_iv_ask !== null) ys.push(p.market_iv_ask);
      }
    if (!xs.length) return null;
    const padY = (Math.max(...ys) - Math.min(...ys)) * 0.12 || 0.01;
    return {
      x0: Math.min(...xs),
      x1: Math.max(...xs),
      y0: Math.max(0, Math.min(...ys) - padY),
      y1: Math.max(...ys) + padY,
    };
  }, [series, axis]);

  if (!domain) {
    return (
      <div className="banner note">
        No solved implied volatilities to plot yet.
      </div>
    );
  }

  const sx = (k: number) =>
    margin.left + ((k - domain.x0) / (domain.x1 - domain.x0 || 1)) * innerW;
  const sy = (v: number) =>
    margin.top + innerH - ((v - domain.y0) / (domain.y1 - domain.y0 || 1)) * innerH;

  const yLabel = axis === "iv" ? "Implied volatility" : "Total variance  w = σ²T";

  return (
    <div>
      <div className="row" style={{ marginBottom: 10 }}>
        <button
          className={axis === "iv" ? "" : "secondary"}
          onClick={() => setAxis("iv")}
        >
          Implied volatility
        </button>
        <button
          className={axis === "variance" ? "" : "secondary"}
          onClick={() => setAxis("variance")}
        >
          Total variance
        </button>
        <span className="muted" style={{ marginLeft: 8 }}>
          {slices.map((slice, index) => (
            <button
              key={slice.expiry}
              className="secondary"
              style={{
                marginRight: 6,
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
        </span>
      </div>

      <svg width={width} height={height} role="img" aria-label="Volatility smile">
        {niceTicks(domain.y0, domain.y1).map((tick) => (
          <g key={`y${tick}`}>
            <line
              x1={margin.left}
              x2={width - margin.right}
              y1={sy(tick)}
              y2={sy(tick)}
              stroke="var(--border)"
            />
            <text
              x={margin.left - 8}
              y={sy(tick) + 4}
              textAnchor="end"
              fontSize="11"
              fill="var(--muted)"
            >
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
            <text
              x={sx(tick)}
              y={height - margin.bottom + 16}
              textAnchor="middle"
              fontSize="11"
              fill="var(--muted)"
            >
              {tick.toFixed(2)}
            </text>
          </g>
        ))}

        <text
          x={margin.left + innerW / 2}
          y={height - 6}
          textAnchor="middle"
          fontSize="11"
          fill="var(--muted)"
        >
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
          {yLabel}
        </text>

        {series.map((s) => {
          const centre = s.points.map(
            (p) => [sx(p.log_moneyness ?? 0), sy(axis === "iv" ? p.market_iv ?? 0 : p.total_variance ?? 0)] as const,
          );
          const envelope =
            axis === "iv" &&
            s.points.every((p) => p.market_iv_bid !== null && p.market_iv_ask !== null)
              ? [
                  ...s.points.map(
                    (p) => `${sx(p.log_moneyness ?? 0)},${sy(p.market_iv_ask ?? 0)}`,
                  ),
                  ...[...s.points]
                    .reverse()
                    .map((p) => `${sx(p.log_moneyness ?? 0)},${sy(p.market_iv_bid ?? 0)}`),
                ].join(" ")
              : null;
          return (
            <g key={s.expiry}>
              {envelope && (
                <polygon points={envelope} fill={s.colour} opacity={0.14} />
              )}
              <polyline
                points={centre.map(([x, y]) => `${x},${y}`).join(" ")}
                fill="none"
                stroke={s.colour}
                strokeWidth={1.5}
              />
              {s.points.map((p, index) => (
                <circle
                  key={`${p.instrument_id}-${index}`}
                  cx={centre[index][0]}
                  cy={centre[index][1]}
                  r={3}
                  fill={s.colour}
                  style={{ cursor: onSelectPoint ? "pointer" : "default" }}
                  onClick={() => onSelectPoint?.(p.instrument_id)}
                >
                  <title>
                    {`K=${p.strike} ${p.option_type}\nIV=${((p.market_iv ?? 0) * 100).toFixed(2)}%` +
                      (p.market_iv_bid !== null && p.market_iv_ask !== null
                        ? `\nenvelope ${(p.market_iv_bid * 100).toFixed(2)}% – ${(p.market_iv_ask * 100).toFixed(2)}%`
                        : "") +
                      `\nk=${(p.log_moneyness ?? 0).toFixed(4)}`}
                  </title>
                </circle>
              ))}
            </g>
          );
        })}
      </svg>

      <p className="muted" style={{ fontSize: 11, marginTop: 4 }}>
        Shaded band is the bid/ask implied-volatility envelope: the width of the
        market in volatility terms. Points are observed quotes only — no fitted
        surface exists until Phase 2.
      </p>
    </div>
  );
}

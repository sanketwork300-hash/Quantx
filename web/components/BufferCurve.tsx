"use client";

import type { LadderPoint, ShortfallRegion } from "@/lib/types";

/**
 * The estimated buffer across a ladder of moves.
 *
 * There is deliberately no liquidation marker. The shaded band is where the
 * *estimated* buffer is at or below zero under the stated model and capital,
 * and the caption says so; a line labelled "liquidation" would be a claim about
 * a broker's behaviour that this platform cannot make.
 */
export function BufferCurve({
  ladder,
  downside,
  upside,
  currency,
}: {
  ladder: LadderPoint[];
  downside: ShortfallRegion | null;
  upside: ShortfallRegion | null;
  currency: string;
}) {
  const points = ladder.filter((point) => point.buffer !== null);
  if (points.length < 2) return null;

  const width = 720;
  const height = 260;
  const pad = { left: 64, right: 16, top: 16, bottom: 36 };

  const xs = points.map((p) => p.spot_return);
  const ys = points.map((p) => p.buffer as number);
  const xMin = Math.min(...xs);
  const xMax = Math.max(...xs);
  const yMin = Math.min(0, ...ys);
  const yMax = Math.max(0, ...ys);

  const x = (value: number) =>
    pad.left + ((value - xMin) / (xMax - xMin || 1)) * (width - pad.left - pad.right);
  const y = (value: number) =>
    height - pad.bottom - ((value - yMin) / (yMax - yMin || 1)) * (height - pad.top - pad.bottom);

  const path = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(p.spot_return)},${y(p.buffer as number)}`).join(" ");
  const zeroY = y(0);

  const bands = [
    downside ? { from: xMin, to: downside.approximate_entry } : null,
    upside ? { from: upside.approximate_entry, to: xMax } : null,
  ].filter(Boolean) as { from: number; to: number }[];

  const format = (value: number) =>
    Math.abs(value) >= 1000
      ? `${(value / 1000).toFixed(0)}k`
      : value.toFixed(0);

  return (
    <div>
      <svg viewBox={`0 0 ${width} ${height}`} style={{ width: "100%", height: "auto" }}>
        {bands.map((band, index) => (
          <rect
            key={index}
            x={x(band.from)}
            y={pad.top}
            width={Math.max(0, x(band.to) - x(band.from))}
            height={height - pad.top - pad.bottom}
            fill="var(--bad, #b4232e)"
            opacity={0.14}
          />
        ))}

        <line
          x1={pad.left}
          x2={width - pad.right}
          y1={zeroY}
          y2={zeroY}
          stroke="currentColor"
          strokeDasharray="4 4"
          opacity={0.5}
        />
        <line
          x1={x(0)}
          x2={x(0)}
          y1={pad.top}
          y2={height - pad.bottom}
          stroke="currentColor"
          strokeDasharray="2 4"
          opacity={0.35}
        />

        <path d={path} fill="none" stroke="currentColor" strokeWidth={2} />
        {points.map((point) => (
          <circle
            key={point.spot_return}
            cx={x(point.spot_return)}
            cy={y(point.buffer as number)}
            r={2.5}
            fill={point.in_shortfall ? "var(--bad, #b4232e)" : "currentColor"}
          />
        ))}

        <text x={pad.left} y={height - 10} fontSize={11} opacity={0.7}>
          {(xMin * 100).toFixed(0)}%
        </text>
        <text x={x(0) - 8} y={height - 10} fontSize={11} opacity={0.7}>
          0
        </text>
        <text x={width - pad.right - 26} y={height - 10} fontSize={11} opacity={0.7}>
          +{(xMax * 100).toFixed(0)}%
        </text>
        <text x={6} y={y(yMax) + 4} fontSize={11} opacity={0.7}>
          {format(yMax)}
        </text>
        <text x={6} y={zeroY + 4} fontSize={11} opacity={0.7}>
          0
        </text>
      </svg>
      <p className="muted" style={{ marginTop: 0 }}>
        Estimated buffer in {currency} against a move in the underlying. The
        shaded band is where the estimated buffer is at or below zero under the
        stated model and capital. It is not a liquidation level, and no such
        level is shown, because this platform does not have your broker&rsquo;s
        rules.
      </p>
    </div>
  );
}

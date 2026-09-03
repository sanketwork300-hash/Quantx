"use client";

import type { ModelValue } from "@/lib/types";

const COLOURS: Record<string, string> = {
  BLACK_SCHOLES_MERTON: "#4c9aff",
  LOCAL_VOL_PDE: "#3fb950",
  HESTON: "#d29922",
  MONTE_CARLO: "#f778ba",
};

interface Props {
  values: ModelValue[];
  referenceValue: number | null;
  marketPrice: number | null;
}

/**
 * Every model's value on one axis, with the range they span shaded.
 *
 * The *range* is the visual subject and the median is a single thin line
 * inside it, which is the point the payload makes in words: where the models
 * disagree, the width of the interval is a better description of what is known
 * about the contract than any number inside it. An observed market price, when
 * there is one, is drawn in a different shape from every model value, so the
 * observation and the estimates cannot be confused.
 */
export function DispersionBar({ values, referenceValue, marketPrice }: Props) {
  const priced = values.filter((value) => value.value !== null);
  if (priced.length === 0 || referenceValue === null) {
    return (
      <div className="banner note">
        No model produced a value, so there is no range to draw. This is an
        absence, not a value of zero.
      </div>
    );
  }

  const points = priced.map((value) => value.value as number);
  const candidates = marketPrice === null ? points : [...points, marketPrice];
  const low = Math.min(...candidates);
  const high = Math.max(...candidates);
  const pad = Math.max((high - low) * 0.18, Math.abs(high) * 0.002, 1e-9);
  const min = low - pad;
  const max = high + pad;

  const width = 760;
  const height = 148;
  const margin = { top: 26, right: 24, bottom: 44, left: 24 };
  const innerW = width - margin.left - margin.right;
  const x = (v: number) => margin.left + ((v - min) / (max - min)) * innerW;

  const modelLow = Math.min(...points);
  const modelHigh = Math.max(...points);
  const axisY = height - margin.bottom;

  return (
    <svg viewBox={`0 0 ${width} ${height}`} role="img" style={{ width: "100%" }}>
      <title>Model values, the range they span, and the observed price</title>

      <rect
        x={x(modelLow)}
        y={margin.top - 6}
        width={Math.max(x(modelHigh) - x(modelLow), 1)}
        height={axisY - margin.top + 12}
        fill="#4c9aff"
        opacity={0.13}
      />
      <line
        x1={margin.left}
        x2={width - margin.right}
        y1={axisY}
        y2={axisY}
        stroke="#39414d"
      />

      {referenceValue !== null && (
        <line
          x1={x(referenceValue)}
          x2={x(referenceValue)}
          y1={margin.top - 6}
          y2={axisY + 6}
          stroke="#8b949e"
          strokeDasharray="3 3"
        />
      )}

      {priced.map((value, index) => (
        <g key={value.model}>
          <circle
            cx={x(value.value as number)}
            cy={margin.top + 14 + index * 16}
            r={5}
            fill={COLOURS[value.model] ?? "#a371f7"}
          />
          <text
            x={x(value.value as number) + 9}
            y={margin.top + 18 + index * 16}
            fontSize={10}
            fill="#c9d1d9"
          >
            {value.model.replaceAll("_", " ").toLowerCase()}
          </text>
        </g>
      ))}

      {marketPrice !== null && (
        <g>
          <path
            d={`M ${x(marketPrice)} ${axisY - 9} l 7 9 l -7 9 l -7 -9 Z`}
            fill="none"
            stroke="#f0f6fc"
            strokeWidth={1.5}
          />
          <text x={x(marketPrice)} y={axisY + 32} fontSize={10} fill="#f0f6fc" textAnchor="middle">
            observed mid
          </text>
        </g>
      )}

      <text x={margin.left} y={height - 8} fontSize={10} fill="#8b949e">
        {min.toFixed(4)}
      </text>
      <text
        x={width - margin.right}
        y={height - 8}
        fontSize={10}
        fill="#8b949e"
        textAnchor="end"
      >
        {max.toFixed(4)}
      </text>
      <text x={x(referenceValue)} y={16} fontSize={10} fill="#8b949e" textAnchor="middle">
        median {referenceValue.toFixed(4)}
      </text>
    </svg>
  );
}

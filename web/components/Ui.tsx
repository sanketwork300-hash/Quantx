"use client";

import type { AnalyticalWarning, Quality } from "@/lib/types";

export function Metric({
  label,
  value,
  unit,
}: {
  label: string;
  value: React.ReactNode;
  unit?: string;
}) {
  return (
    <div className="card metric">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {unit ? <div className="unit">{unit}</div> : null}
    </div>
  );
}

/** Colour follows the score, so a poor dimension is visible without reading it. */
export function ScoreTag({ score, label }: { score: number; label?: string }) {
  const tone = score >= 0.75 ? "good" : score >= 0.4 ? "warn" : "bad";
  return (
    <span className={`tag ${tone}`}>
      {label ? `${label} ` : ""}
      {score.toFixed(2)}
    </span>
  );
}

export function SeverityTag({ severity }: { severity: string }) {
  const tone =
    severity === "ERROR" ? "bad" : severity === "WARNING" ? "warn" : "info";
  return <span className={`tag ${tone}`}>{severity}</span>;
}

export function Warnings({ warnings }: { warnings: AnalyticalWarning[] }) {
  if (!warnings.length) return null;
  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>What the platform could not do</h3>
      {warnings.map((warning) => (
        <div key={warning.code + warning.message} style={{ marginBottom: 8 }}>
          <SeverityTag severity={warning.severity} />{" "}
          <span className="mono">{warning.code}</span>
          <div className="muted">{warning.message}</div>
        </div>
      ))}
    </div>
  );
}

/**
 * The explanation panel. Every number a user might act on has to be traceable
 * to the specific measurements that produced it.
 */
export function QualityBreakdown({ quality }: { quality: Quality }) {
  const rows: [string, number, string][] = [
    ["Freshness", quality.stale_score, "quote age against an asset-class half-life"],
    ["Spread", quality.spread_score, "relative spread against a reference spread"],
    ["Liquidity", quality.liquidity_score, "volume, open interest and quoted size"],
    ["Consistency", quality.consistency_score, "crossed, locked, bounds, jumps"],
    ["Completeness", quality.completeness_score, "expected fields present"],
  ];
  return (
    <div>
      <table>
        <tbody>
          {rows.map(([label, score, why]) => (
            <tr key={label}>
              <td>
                {label}
                <div className="muted" style={{ fontSize: 11 }}>
                  {why}
                </div>
              </td>
              <td style={{ width: 90 }}>
                <ScoreTag score={score} />
              </td>
            </tr>
          ))}
          <tr>
            <td>
              <strong>Overall</strong>
              <div className="muted" style={{ fontSize: 11 }}>
                weighted geometric mean, so one bad dimension is not averaged away
              </div>
            </td>
            <td>
              <ScoreTag score={quality.overall_score} />
            </td>
          </tr>
        </tbody>
      </table>
      {quality.flags.length > 0 && (
        <>
          <h3>Flags</h3>
          <ul className="reasons">
            {quality.flags.map((flag, index) => (
              <li key={`${flag.code}-${index}`}>
                <SeverityTag severity={flag.severity} />{" "}
                <span className="mono">{flag.code}</span>
                <div className="muted">{flag.message}</div>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

export function ErrorBanner({ error }: { error: unknown }) {
  if (!error) return null;
  const problem = (error as { problem?: { code: string; detail: string | null } })
    .problem;
  return (
    <div className="banner error">
      {problem ? (
        <>
          <span className="mono">{problem.code}</span> — {problem.detail}
        </>
      ) : (
        String((error as Error).message ?? error)
      )}
    </div>
  );
}

export function Disclaimer() {
  return (
    <div className="disclaimer">
      This is an analytics and research platform. It produces reference values,
      estimated margin, estimated slippage and counterfactual simulations, each
      with a stated model confidence and full provenance. It does not produce
      trade recommendations, fair values, guaranteed liquidation levels or
      broker-equivalent margin, and a deviation from a model is not an
      arbitrage.
    </div>
  );
}

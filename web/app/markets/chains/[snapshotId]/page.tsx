"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useMemo, useState } from "react";
import { api } from "@/lib/api";
import {
  Disclaimer,
  ErrorBanner,
  Metric,
  QualityBreakdown,
  ScoreTag,
  Warnings,
} from "@/components/Ui";
import type { ChainSnapshot, Envelope, OptionQuote } from "@/lib/types";

function num(value: string | null): string {
  return value === null ? "—" : value;
}

export default function ChainDetailPage() {
  const params = useParams<{ snapshotId: string }>();
  const [expiry, setExpiry] = useState<string>("");
  const [showExcluded, setShowExcluded] = useState(true);
  const [selected, setSelected] = useState<OptionQuote | null>(null);

  const chain = useQuery({
    queryKey: ["chain", params.snapshotId],
    queryFn: () =>
      api.get<Envelope<ChainSnapshot>>(
        `/market/chains/${params.snapshotId}?include_excluded=true`,
      ),
  });

  const results = chain.data?.results;
  const quotes = useMemo(() => {
    if (!results) return [];
    return results.quotes.filter(
      (quote) =>
        (expiry === "" || quote.expiry === expiry) &&
        (showExcluded || !quote.excluded),
    );
  }, [results, expiry, showExcluded]);

  return (
    <>
      <h2>Chain snapshot</h2>
      <p className="subtitle">
        Observed quotes with their data-quality scores. Excluded quotes are
        shown, never hidden, and each carries the reason it was set aside.
      </p>

      <ErrorBanner error={chain.error} />

      {results && (
        <>
          <div className="grid">
            <Metric label="As of" value={new Date(results.as_of_timestamp).toISOString().slice(0, 19)} unit="UTC" />
            <Metric label="Underlying price" value={num(results.underlying_price)} unit="observed" />
            <Metric label="Rows in" value={results.counts.input} />
            <Metric label="Kept" value={results.counts.kept} />
            <Metric label="Excluded" value={results.counts.excluded} />
            <Metric label="Rejected" value={results.counts.rejected} unit="could not form a quote" />
          </div>

          <Warnings warnings={chain.data!.warnings} />

          <div className="card">
            <div className="row">
              <Link
                className="button"
                href={`/markets/chains/${params.snapshotId}/smile`}
                style={{ textDecoration: "none" }}
              >
                Implied volatility →
              </Link>
              <div className="field" style={{ marginBottom: 0 }}>
                <label htmlFor="expiry">Expiry</label>
                <select
                  id="expiry"
                  value={expiry}
                  onChange={(event) => setExpiry(event.target.value)}
                >
                  <option value="">All ({results.expiries.length})</option>
                  {results.expiries.map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </select>
              </div>
              <label style={{ marginBottom: 0 }}>
                <input
                  type="checkbox"
                  checked={showExcluded}
                  onChange={(event) => setShowExcluded(event.target.checked)}
                />{" "}
                Show excluded quotes
              </label>
              <span className="muted">{quotes.length} rows</span>
            </div>
          </div>

          <div style={{ display: "flex", gap: 16, alignItems: "flex-start" }}>
            <div className="table-wrap" style={{ flex: 1 }}>
              <table>
                <thead>
                  <tr>
                    <th>Expiry</th>
                    <th>Strike</th>
                    <th>Type</th>
                    <th>Bid</th>
                    <th>Ask</th>
                    <th>Mid</th>
                    <th>Rel. spread</th>
                    <th>Volume</th>
                    <th>OI</th>
                    <th>Quality</th>
                    <th>Excluded because</th>
                  </tr>
                </thead>
                <tbody>
                  {quotes.map((quote) => (
                    <tr
                      key={quote.instrument_id + quote.source_row_number}
                      className={`${quote.excluded ? "excluded" : ""} ${
                        selected?.instrument_id === quote.instrument_id &&
                        selected?.source_row_number === quote.source_row_number
                          ? "selected"
                          : ""
                      }`}
                      onClick={() => setSelected(quote)}
                      style={{ cursor: "pointer" }}
                    >
                      <td>{quote.expiry}</td>
                      <td>{quote.strike}</td>
                      <td>{quote.option_type === "CALL" ? "C" : "P"}</td>
                      <td>{num(quote.bid_price)}</td>
                      <td>{num(quote.ask_price)}</td>
                      <td>{num(quote.mid_price)}</td>
                      <td>
                        {quote.relative_spread != null
                          ? `${(quote.relative_spread * 100).toFixed(2)}%`
                          : "—"}
                      </td>
                      <td>{num(quote.volume)}</td>
                      <td>{num(quote.open_interest)}</td>
                      <td>
                        <ScoreTag score={quote.quality.overall_score} />
                      </td>
                      <td className="mono">{quote.exclusion_reason ?? ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="card" style={{ width: 380, flex: "0 0 380px" }}>
              <h3 style={{ marginTop: 0 }}>
                {selected ? "Why this score?" : "Select a quote"}
              </h3>
              {selected ? (
                <>
                  <p className="mono">
                    {selected.expiry} {selected.strike} {selected.option_type}
                    {selected.source_row_number
                      ? ` · source row ${selected.source_row_number}`
                      : ""}
                  </p>
                  {selected.excluded && (
                    <div className="banner error">
                      Excluded: <span className="mono">{selected.exclusion_reason}</span>
                    </div>
                  )}
                  <QualityBreakdown quality={selected.quality} />
                </>
              ) : (
                <p className="muted">
                  Click a row to see the measurements behind its quality score
                  and, where applicable, the reason it was excluded.
                </p>
              )}
            </div>
          </div>

          <div className="card" style={{ marginTop: 16 }}>
            <h3 style={{ marginTop: 0 }}>Provenance</h3>
            <p className="muted" style={{ marginTop: 0 }}>
              What this snapshot was computed from, so it can be reproduced later.
            </p>
            <table>
              <tbody>
                <tr>
                  <td>Market state timestamp</td>
                  <td className="mono">
                    {chain.data!.provenance.market_state_timestamp}
                  </td>
                </tr>
                <tr>
                  <td>Sources</td>
                  <td className="mono">
                    {chain.data!.provenance.market_data_sources.join(", ")}
                  </td>
                </tr>
                <tr>
                  <td>Model versions</td>
                  <td className="mono">
                    {Object.entries(chain.data!.provenance.model_versions)
                      .map(([key, value]) => `${key}=${value}`)
                      .join(", ")}
                  </td>
                </tr>
                <tr>
                  <td>Code commit</td>
                  <td className="mono">{chain.data!.provenance.code_commit}</td>
                </tr>
                <tr>
                  <td>Dataset digest</td>
                  <td className="mono">{results.dataset_digest ?? "—"}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </>
      )}

      <Disclaimer />
    </>
  );
}

"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { api } from "@/lib/api";
import { Disclaimer, ErrorBanner, Metric } from "@/components/Ui";
import type { ChainSnapshotSummary } from "@/lib/types";

interface Version {
  name: string;
  version: string;
  code_commit: string;
  environment: string;
}

export default function Dashboard() {
  const version = useQuery({
    queryKey: ["version"],
    queryFn: () => api.get<Version>("/meta/version"),
  });
  const chains = useQuery({
    queryKey: ["chains"],
    queryFn: () => api.get<ChainSnapshotSummary[]>("/market/chains"),
    retry: false,
  });

  const latest = chains.data?.[0];

  return (
    <>
      <h2>Dashboard</h2>
      <p className="subtitle">
        What is this position approximately worth, what risk does it add, and
        what will it probably cost to execute?
      </p>

      <ErrorBanner error={version.error} />

      <div className="grid">
        <Metric
          label="Platform"
          value={version.data?.version ?? "…"}
          unit={version.data?.environment}
        />
        <Metric
          label="Chain snapshots"
          value={chains.data?.length ?? (chains.error ? "—" : "…")}
          unit={chains.error ? "sign in to view" : "imported"}
        />
        <Metric
          label="Latest snapshot quality"
          value={
            latest?.overall_score != null ? latest.overall_score.toFixed(2) : "—"
          }
          unit="0–1, higher is better"
        />
        <Metric
          label="Quotes kept"
          value={latest ? latest.counts.kept : "—"}
          unit={latest ? `of ${latest.counts.input} rows` : undefined}
        />
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Where the platform is</h3>
        <p className="muted" style={{ marginTop: 0 }}>
          Phases 0 to 9 are complete: the instrument master and ingestion
          pipeline, implied volatilities and forwards, SVI surface calibration,
          the surface-deviation scanner, portfolio valuation with Greeks by
          group, Value at Risk and scenario stress by full repricing, margin
          estimation with an estimated-shortfall region, transaction cost
          analysis on your own trade log, counterfactual execution simulation,
          and the advanced derivatives stack — an arbitrage-free SSVI surface,
          Dupire local volatility, Heston, and a model consensus that reports a
          range rather than a price. Microstructure follows in Phase 10.
        </p>
        <ul className="reasons">
          <li>
            <Link href="/data">Import an option chain</Link> — upload, preview
            the column mapping, then ingest.
          </li>
          <li>
            <Link href="/markets/chains">Browse chain snapshots</Link> — every
            quote with its quality scores, and every exclusion with its reason.
          </li>
          <li>
            <Link href="/markets/surfaces">Calibrated surfaces</Link> — fitted
            slices with their arbitrage checks and reference values.
          </li>
          <li>
            <Link href="/markets/global-surfaces">Global surfaces</Link> — one
            SSVI fit across every expiry, which cannot contain calendar
            arbitrage, with the Dupire local-volatility grid and the implied
            density derived from it.
          </li>
          <li>
            <Link href="/markets/consensus">Model consensus</Link> — one
            contract priced by four models, showing the range they span before
            the median inside it.
          </li>
          <li>
            <Link href="/portfolios">Portfolios</Link> — import positions, value
            them against one snapshot, and read the Greeks by underlying,
            expiry, asset class or strategy tag.
          </li>
          <li>
            <Link href="/execution">Execution</Link> — import a trade log and
            benchmark it. Every benchmark states its window, source and method,
            and refuses rather than averaging a handful of ticks.
          </li>
          <li>
            <Link href="/execution/simulate">Execution simulation</Link> — price
            TWAP, VWAP, POV and liquidity-adaptive schedules against a past path.
            Every number is a counterfactual estimate and says so.
          </li>
          <li>
            <Link href="/scenarios">Scenarios</Link> — shipped hypotheticals, your
            own, or one derived from an underlying&rsquo;s recorded history. Apply
            one and the book is fully repriced, not extrapolated from its Greeks.
          </li>
        </ul>
      </div>

      <Disclaimer />
    </>
  );
}

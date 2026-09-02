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
          Phase 0 is complete: instrument master, market-data providers, the
          data-quality engine, the option-chain ingestion pipeline, asynchronous
          jobs and authentication. Implied volatility and everything downstream
          of it begins in Phase 1, deliberately after this foundation is tested.
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
        </ul>
      </div>

      <Disclaimer />
    </>
  );
}

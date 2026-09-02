"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { api } from "@/lib/api";
import { Disclaimer, ErrorBanner } from "@/components/Ui";
import type { AnalysisSummary } from "@/lib/types";

export default function AnalysesPage() {
  const analyses = useQuery({
    queryKey: ["analyses"],
    queryFn: () => api.get<AnalysisSummary[]>("/derivatives/analyses"),
  });

  return (
    <>
      <h2>Volatility analyses</h2>
      <p className="subtitle">
        Each run records the curve, day count and settlement time it used, so an
        older analysis can be reproduced rather than merely re-run.
      </p>

      <ErrorBanner error={analyses.error} />

      {analyses.data?.length === 0 && (
        <div className="banner note">
          No analyses yet. Open a{" "}
          <Link href="/markets/chains">chain snapshot</Link> and run one.
        </div>
      )}

      {analyses.data && analyses.data.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Run at</th>
                <th>As of</th>
                <th>Quotes</th>
                <th>Solved</th>
                <th>Expiries</th>
                <th>Curve</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {analyses.data.map((row) => (
                <tr key={row.analysis_id}>
                  <td>{new Date(row.created_at).toLocaleString()}</td>
                  <td>{new Date(row.as_of_timestamp).toISOString()}</td>
                  <td>{row.quotes_in}</td>
                  <td>{row.quotes_solved}</td>
                  <td>{row.expiries}</td>
                  <td className="mono">{row.curve_id ?? "—"}</td>
                  <td>
                    <Link href={`/markets/chains/${row.snapshot_id}/smile`}>
                      Open smile
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <Disclaimer />
    </>
  );
}

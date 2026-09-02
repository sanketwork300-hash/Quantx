"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { api } from "@/lib/api";
import { Disclaimer, ErrorBanner, ScoreTag } from "@/components/Ui";
import type { ChainSnapshotSummary } from "@/lib/types";

export default function ChainListPage() {
  const chains = useQuery({
    queryKey: ["chains"],
    queryFn: () => api.get<ChainSnapshotSummary[]>("/market/chains"),
  });

  return (
    <>
      <h2>Option chain snapshots</h2>
      <p className="subtitle">
        Each ingestion is its own observation snapshot. Observations are
        append-only: re-importing never overwrites what was previously observed.
      </p>

      <ErrorBanner error={chains.error} />

      {chains.data?.length === 0 && (
        <div className="banner note">
          No snapshots yet. <Link href="/data">Import an option chain</Link>.
        </div>
      )}

      {chains.data && chains.data.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>As of</th>
                <th>Source</th>
                <th>Rows</th>
                <th>Kept</th>
                <th>Excluded</th>
                <th>Rejected</th>
                <th>Quality</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {chains.data.map((snapshot) => (
                <tr key={snapshot.snapshot_id}>
                  <td>{new Date(snapshot.as_of_timestamp).toISOString()}</td>
                  <td className="mono">{snapshot.source}</td>
                  <td>{snapshot.counts.input}</td>
                  <td>{snapshot.counts.kept}</td>
                  <td>{snapshot.counts.excluded}</td>
                  <td>{snapshot.counts.rejected}</td>
                  <td>
                    {snapshot.overall_score != null ? (
                      <ScoreTag score={snapshot.overall_score} />
                    ) : (
                      "—"
                    )}
                  </td>
                  <td>
                    <Link href={`/markets/chains/${snapshot.snapshot_id}`}>
                      Open
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

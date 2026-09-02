"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { api } from "@/lib/api";
import { Disclaimer, ErrorBanner } from "@/components/Ui";
import type { SurfaceSummary } from "@/lib/types";

export default function SurfacesPage() {
  const surfaces = useQuery({
    queryKey: ["surfaces"],
    queryFn: () => api.get<SurfaceSummary[]>("/derivatives/surfaces"),
  });

  return (
    <>
      <h2>Volatility surfaces</h2>
      <p className="subtitle">
        Each surface is content-addressed: two rows with the same id were fitted
        from the same numbers, so a provenance record naming one identifies
        exactly which model produced a reference value.
      </p>

      <ErrorBanner error={surfaces.error} />

      {surfaces.data?.length === 0 && (
        <div className="banner note">
          No surfaces yet. Open a <Link href="/markets/chains">chain snapshot</Link>,
          run the implied-volatility analysis, then fit one.
        </div>
      )}

      {surfaces.data && surfaces.data.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Fitted at</th>
                <th>As of</th>
                <th>Model</th>
                <th>Slices</th>
                <th>Surface id</th>
                <th>Curve</th>
              </tr>
            </thead>
            <tbody>
              {surfaces.data.map((row) => (
                <tr key={row.surface_row_id}>
                  <td>{new Date(row.created_at).toLocaleString()}</td>
                  <td>{new Date(row.as_of_timestamp).toISOString().slice(0, 19)}</td>
                  <td>
                    {row.model}
                    <div className="muted" style={{ fontSize: 11 }}>
                      {row.model_version}
                    </div>
                  </td>
                  <td>
                    {row.slices_fitted} / {row.slices_total}
                  </td>
                  <td className="mono">{row.surface_id}</td>
                  <td className="mono">{row.curve_id ?? "—"}</td>
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

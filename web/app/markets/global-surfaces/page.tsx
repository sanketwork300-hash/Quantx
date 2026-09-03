"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { api } from "@/lib/api";
import { Disclaimer, ErrorBanner } from "@/components/Ui";
import type { GlobalSurfaceSummary } from "@/lib/types";

export default function GlobalSurfacesPage() {
  const surfaces = useQuery({
    queryKey: ["global-surfaces"],
    queryFn: () => api.get<GlobalSurfaceSummary[]>("/derivatives/global-surfaces"),
  });

  return (
    <>
      <h2>Global surfaces</h2>
      <p className="subtitle">
        SSVI: three shape parameters for the whole surface plus one at-the-money
        variance per expiry. Requiring that variance to be non-decreasing{" "}
        <em>is</em> the no-calendar-arbitrage condition, so an admissible fit
        cannot contain the violation the per-expiry SVI surface could only
        report. The two live side by side — SVI still describes any single smile
        better, and the difference between them is itself informative.
      </p>

      <ErrorBanner error={surfaces.error} />

      {surfaces.data?.length === 0 && (
        <div className="banner note">
          No global surfaces yet. Open a{" "}
          <Link href="/markets/analyses">volatility analysis</Link> and fit one.
        </div>
      )}

      {surfaces.data && surfaces.data.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Fitted at</th>
                <th>As of</th>
                <th>Status</th>
                <th>rho / eta / gamma</th>
                <th>Expiries</th>
                <th>RMSE (vol pts)</th>
                <th>Calendar-free</th>
                <th>min g</th>
              </tr>
            </thead>
            <tbody>
              {surfaces.data.map((row) => (
                <tr key={row.global_surface_row_id}>
                  <td>
                    <Link href={`/markets/global-surfaces/${row.global_surface_row_id}`}>
                      {new Date(row.created_at).toLocaleString()}
                    </Link>
                  </td>
                  <td>{new Date(row.as_of_timestamp).toISOString().slice(0, 19)}</td>
                  <td>{row.status}</td>
                  <td className="mono">
                    {row.parameters
                      ? `${row.parameters.rho.toFixed(3)} / ${row.parameters.eta.toFixed(3)} / ${row.parameters.gamma.toFixed(3)}`
                      : "—"}
                  </td>
                  <td>{row.n_slices}</td>
                  <td>{row.rmse_vol_points?.toFixed(3) ?? "—"}</td>
                  <td>{row.calendar_arbitrage_free ? "yes" : "no"}</td>
                  <td className="mono">{row.min_durrleman_g?.toExponential(2) ?? "—"}</td>
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

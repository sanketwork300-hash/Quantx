"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { api } from "@/lib/api";
import { Disclaimer, ErrorBanner } from "@/components/Ui";
import type { Portfolio } from "@/lib/types";

export default function PortfoliosPage() {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [currency, setCurrency] = useState("INR");
  const [description, setDescription] = useState("");

  const portfolios = useQuery({
    queryKey: ["portfolios"],
    queryFn: () => api.get<Portfolio[]>("/portfolios"),
  });

  const create = useMutation({
    mutationFn: () =>
      api.post<Portfolio>("/portfolios", {
        name,
        base_currency: currency.toUpperCase(),
        description: description || null,
      }),
    onSuccess: () => {
      setName("");
      setDescription("");
      queryClient.invalidateQueries({ queryKey: ["portfolios"] });
    },
  });

  return (
    <>
      <h2>Portfolios</h2>
      <p className="subtitle">
        A portfolio is valued against one market snapshot, so every number in a
        report comes from the same moment.
      </p>

      <ErrorBanner error={create.error ?? portfolios.error} />

      <div className="card">
        <h3 style={{ marginTop: 0 }}>New portfolio</h3>
        <div className="row">
          <div className="field">
            <label htmlFor="name">Name</label>
            <input
              id="name"
              value={name}
              placeholder="Vol book"
              onChange={(event) => setName(event.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="currency">Base currency</label>
            <input
              id="currency"
              value={currency}
              maxLength={3}
              onChange={(event) => setCurrency(event.target.value)}
            />
          </div>
          <div className="field" style={{ flex: 1 }}>
            <label htmlFor="description">Description</label>
            <input
              id="description"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </div>
        </div>
        <button
          onClick={() => create.mutate()}
          disabled={!name.trim() || currency.length !== 3 || create.isPending}
        >
          {create.isPending ? "Creating…" : "Create"}
        </button>
        <p className="muted" style={{ marginBottom: 0 }}>
          Positions in another currency are converted at the rate in the same
          snapshot as the prices, and the rate used is recorded on each position.
        </p>
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Your portfolios</h3>
        {portfolios.data?.length === 0 && (
          <p className="muted">Nothing yet. Create one above, then import positions.</p>
        )}
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Base currency</th>
                <th>Description</th>
                <th>Created</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(portfolios.data ?? []).map((portfolio) => (
                <tr key={portfolio.id}>
                  <td>
                    <Link href={`/portfolios/${portfolio.id}`}>{portfolio.name}</Link>
                  </td>
                  <td>{portfolio.base_currency}</td>
                  <td className="muted">{portfolio.description ?? "—"}</td>
                  <td>{new Date(portfolio.created_at).toLocaleString()}</td>
                  <td>
                    <Link href={`/portfolios/${portfolio.id}/import`}>Import positions →</Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <Disclaimer />
    </>
  );
}

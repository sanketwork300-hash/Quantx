"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import { Disclaimer, ErrorBanner } from "@/components/Ui";
import type { Portfolio, RiskFactorKind, Scenario, ShockType } from "@/lib/types";

const KINDS: { id: RiskFactorKind; label: string; types: ShockType[] }[] = [
  { id: "UNDERLYING_PRICE", label: "Underlying price", types: ["PERCENTAGE", "ABSOLUTE"] },
  { id: "VOLATILITY", label: "Volatility", types: ["VOL_POINTS", "PERCENTAGE"] },
  { id: "RISK_FREE_RATE", label: "Risk-free rate", types: ["BASIS_POINTS", "ABSOLUTE"] },
];

function SourceTag({ source }: { source: Scenario["source"] }) {
  const tone =
    source === "DERIVED_FROM_HISTORY" ? "good" : source === "USER_DEFINED" ? "info" : "warn";
  return <span className={`tag ${tone}`}>{source.toLowerCase().replace(/_/g, " ")}</span>;
}

export default function ScenariosPage() {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [kind, setKind] = useState<RiskFactorKind>("UNDERLYING_PRICE");
  const [shockType, setShockType] = useState<ShockType>("PERCENTAGE");
  const [value, setValue] = useState("-0.10");
  const [deriveName, setDeriveName] = useState("");
  const [underlyingId, setUnderlyingId] = useState("");
  const [window, setWindow] = useState("1");

  const scenarios = useQuery({
    queryKey: ["scenarios"],
    queryFn: () => api.get<Scenario[]>("/scenarios"),
  });

  const portfolios = useQuery({
    queryKey: ["portfolios"],
    queryFn: () => api.get<Portfolio[]>("/portfolios"),
  });

  const create = useMutation({
    mutationFn: () =>
      api.post<Scenario>("/scenarios", {
        name,
        shocks: [{ kind, shock_type: shockType, value: Number(value) }],
      }),
    onSuccess: () => {
      setName("");
      queryClient.invalidateQueries({ queryKey: ["scenarios"] });
    },
  });

  const derive = useMutation({
    mutationFn: () =>
      api.post<Scenario>("/scenarios/derive", {
        name: deriveName,
        underlying_id: underlyingId,
        window_days: Number(window) || 1,
      }),
    onSuccess: () => {
      setDeriveName("");
      queryClient.invalidateQueries({ queryKey: ["scenarios"] });
    },
  });

  const types = KINDS.find((item) => item.id === kind)?.types ?? [];

  return (
    <>
      <h2>Scenarios</h2>
      <p className="subtitle">
        A scenario is a named set of shocks. Applying one produces a shocked
        market and the portfolio is fully revalued against it.
      </p>

      <ErrorBanner error={create.error ?? derive.error ?? scenarios.error} />

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Where a scenario&rsquo;s numbers come from</h3>
        <ul className="reasons">
          <li>
            <span className="tag warn">hypothetical</span> — shipped templates.
            Round numbers chosen for illustration. They make no claim about any
            market&rsquo;s past, and none of them is named after a real event.
          </li>
          <li>
            <span className="tag info">user defined</span> — shocks you entered.
            Recorded as yours, whatever they represent.
          </li>
          <li>
            <span className="tag good">derived from history</span> — computed
            below from a series this platform actually holds. Only these carry a
            date range and an event date, and only these are historical claims.
          </li>
        </ul>
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Define a scenario</h3>
        <div className="row">
          <div className="field" style={{ flex: 1 }}>
            <label htmlFor="name">Name</label>
            <input
              id="name"
              value={name}
              placeholder="Gap down"
              onChange={(event) => setName(event.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="kind">Risk factor</label>
            <select
              id="kind"
              value={kind}
              onChange={(event) => {
                const next = event.target.value as RiskFactorKind;
                setKind(next);
                setShockType(KINDS.find((item) => item.id === next)!.types[0]);
              }}
            >
              {KINDS.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.label}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="type">Shock type</label>
            <select
              id="type"
              value={shockType}
              onChange={(event) => setShockType(event.target.value as ShockType)}
            >
              {types.map((item) => (
                <option key={item} value={item}>
                  {item.toLowerCase().replace(/_/g, " ")}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="value">Value</label>
            <input id="value" value={value} onChange={(e) => setValue(e.target.value)} />
          </div>
        </div>
        <p className="muted" style={{ marginTop: 0 }}>
          A percentage is a fraction (−0.10 is a 10% fall), volatility points are
          absolute (0.05 is five points), and basis points are basis points.
          A shock type that makes no sense for the factor is refused rather than
          guessed at.
        </p>
        <button onClick={() => create.mutate()} disabled={!name.trim() || create.isPending}>
          {create.isPending ? "Saving…" : "Save scenario"}
        </button>
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Derive one from recorded history</h3>
        <p className="muted" style={{ marginTop: 0 }}>
          Finds the worst move the underlying&rsquo;s own recorded series
          contains, and records the series, its date range and the date of that
          move. History accumulates one observation per ingested option chain,
          so a short record produces a refusal rather than a number.
        </p>
        <div className="row">
          <div className="field" style={{ flex: 1 }}>
            <label htmlFor="dname">Name</label>
            <input
              id="dname"
              value={deriveName}
              placeholder="Worst recorded day"
              onChange={(event) => setDeriveName(event.target.value)}
            />
          </div>
          <div className="field" style={{ flex: 1 }}>
            <label htmlFor="uid">Underlying id</label>
            <input
              id="uid"
              value={underlyingId}
              placeholder="uuid of the index or equity"
              onChange={(event) => setUnderlyingId(event.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="window">Window (days)</label>
            <input id="window" value={window} onChange={(e) => setWindow(e.target.value)} />
          </div>
        </div>
        <button
          onClick={() => derive.mutate()}
          disabled={!deriveName.trim() || !underlyingId.trim() || derive.isPending}
        >
          {derive.isPending ? "Deriving…" : "Derive from history"}
        </button>
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Available scenarios</h3>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Source</th>
                <th>Shocks</th>
                <th>Derived from</th>
              </tr>
            </thead>
            <tbody>
              {(scenarios.data ?? []).map((item) => (
                <tr key={item.id}>
                  <td>{item.name}</td>
                  <td>
                    <SourceTag source={item.source} />
                  </td>
                  <td>
                    {item.shocks.map((shock) => (
                      <span key={`${shock.kind}-${shock.label}`} className="tag info">
                        {shock.kind.toLowerCase().replace(/_/g, " ")} {shock.label}
                      </span>
                    ))}
                  </td>
                  <td className="muted">
                    {item.derivation
                      ? `${item.derivation.series}, ${item.derivation.observations} obs, ` +
                        `event ${item.derivation.event_date}`
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {(portfolios.data ?? []).length > 0 && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Apply one</h3>
          <ul className="reasons">
            {(portfolios.data ?? []).map((item) => (
              <li key={item.id}>
                <a href={`/portfolios/${item.id}/risk`}>{item.name}</a> — run VaR
                and the stress lab.
              </li>
            ))}
          </ul>
        </div>
      )}

      <Disclaimer />
    </>
  );
}

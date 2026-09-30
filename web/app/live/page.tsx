"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { ErrorBanner, ScoreTag } from "@/components/Ui";
import type {
  FeedStatus,
  Instrument,
  LiveQuote,
  LiveQuotes,
  LiveStatus,
  Subscription,
} from "@/lib/types";

/** How often the page asks for prices. The feed itself runs at its own rate in
 * the worker; this is only how fast the screen is redrawn. */
const REFRESH_MS = 1000;

/** Subscriptions expire unless renewed, so a tab left open keeps its own alive
 * and a tab that is closed stops costing one. */
const RENEW_MS = 60_000;

function StatusTag({ status }: { status: FeedStatus }) {
  const tone =
    status === "CONNECTED"
      ? "good"
      : status === "STALE" || status === "RECONNECTING"
        ? "warn"
        : "bad";
  return <span className={`tag ${tone}`}>{status.toLowerCase()}</span>;
}

/**
 * Age is shown for every price, in words as well as seconds. A live screen with
 * no visible age is the reason a stale quote gets traded on.
 */
function Age({ seconds }: { seconds: number }) {
  const tone = seconds < 5 ? "good" : seconds < 60 ? "warn" : "bad";
  const text =
    seconds < 1
      ? "just now"
      : seconds < 60
        ? `${seconds.toFixed(0)}s ago`
        : `${(seconds / 60).toFixed(1)} min ago`;
  return <span className={`tag ${tone}`}>{text}</span>;
}

function num(value: string | null, digits = 2) {
  if (value === null) return "—";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }) : value;
}

function QuoteRow({ quote }: { quote: LiveQuote }) {
  return (
    <tr>
      <td>
        <strong>{quote.symbol}</strong>
        <div className="muted" style={{ fontSize: 11 }}>
          {quote.exchange} · {quote.asset_class.toLowerCase()}
        </div>
      </td>
      <td className="mono">{num(quote.last_price)}</td>
      <td className="mono">{num(quote.bid_price)}</td>
      <td className="mono">{num(quote.ask_price)}</td>
      {/* Null when there is no genuine two-sided market. Never the last print
          standing in for one. */}
      <td className="mono">{num(quote.mid_price)}</td>
      <td className="mono">{num(quote.volume, 0)}</td>
      <td className="mono">{num(quote.open_interest, 0)}</td>
      <td>
        <Age seconds={quote.age_seconds} />
      </td>
      <td>
        {quote.quality ? (
          <ScoreTag score={quote.quality.overall_score} />
        ) : (
          <span className="muted">not scored</span>
        )}
      </td>
    </tr>
  );
}

export default function LivePage() {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<string[]>([]);

  const status = useQuery({
    queryKey: ["live-status"],
    queryFn: () => api.get<LiveStatus>("/live/status"),
    refetchInterval: 5000,
  });

  const instruments = useQuery({
    queryKey: ["live-instruments"],
    queryFn: () =>
      api.get<{ items: Instrument[] }>(
        "/instruments?asset_class=INDEX&limit=100",
      ),
  });

  const quotes = useQuery({
    queryKey: ["live-quotes", selected],
    enabled: selected.length > 0,
    refetchInterval: REFRESH_MS,
    queryFn: () =>
      api.get<LiveQuotes>(
        `/live/quotes?${selected.map((id) => `instrument_ids=${id}`).join("&")}`,
      ),
  });

  const subscribe = useMutation({
    mutationFn: (instrument_ids: string[]) =>
      api.post<Subscription>("/live/subscriptions", { instrument_ids }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["live-status"] }),
  });

  // Renew while the tab is open. The server lets interest lapse otherwise,
  // which is what stops a closed tab holding a subscription open forever.
  useEffect(() => {
    if (selected.length === 0) return;
    const timer = setInterval(() => subscribe.mutate(selected), RENEW_MS);
    return () => clearInterval(timer);
  }, [selected, subscribe]);

  const toggle = (id: string) => {
    const next = selected.includes(id)
      ? selected.filter((item) => item !== id)
      : [...selected, id];
    setSelected(next);
    if (next.length) subscribe.mutate(next);
  };

  const refresh = useMutation({
    mutationFn: () => api.post<{ job_id: string }>("/live/instruments/refresh"),
  });

  return (
    <>
      <h1>Live market</h1>
      <p className="subtitle">
        Prices as the feed last delivered them, each with its own age. Nothing
        here is a recommendation and nothing is smoothed — an absent value is
        shown as absent.
      </p>

      <ErrorBanner error={quotes.error ?? subscribe.error ?? refresh.error} />

      {status.data?.unavailable_reason ? (
        <div className="card">
          <h2 style={{ marginTop: 0 }}>The feed is not delivering</h2>
          <p className="muted">{status.data.unavailable_reason}</p>
        </div>
      ) : null}

      {status.data ? (
        <div className="card">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <h2 style={{ margin: 0 }}>
              {status.data.provider}{" "}
              {status.data.health ? (
                <StatusTag status={status.data.health.status} />
              ) : (
                <span className="tag bad">no worker</span>
              )}
            </h2>
            <button
              className="secondary"
              disabled={refresh.isPending}
              onClick={() => refresh.mutate()}
            >
              {refresh.isPending ? "Submitting…" : "Reload instruments"}
            </button>
          </div>
          <table>
            <tbody>
              <tr>
                <td>Transport</td>
                <td>
                  {status.data.transport}
                  {!status.data.delivers_every_update ? (
                    <div className="muted" style={{ fontSize: 11 }}>
                      Samples the market
                      {status.data.poll_interval_seconds
                        ? ` every ${status.data.poll_interval_seconds}s`
                        : ""}
                      . The latest state is shown at that rate; individual ticks
                      between samples were not seen.
                    </div>
                  ) : null}
                </td>
              </tr>
              {status.data.health ? (
                <>
                  <tr>
                    <td>Messages received</td>
                    <td className="mono">
                      {status.data.health.events_received.toLocaleString()}
                    </td>
                  </tr>
                  <tr>
                    <td>Reconnects</td>
                    <td className="mono">{status.data.health.reconnects}</td>
                  </tr>
                  {status.data.health.last_error ? (
                    <tr>
                      <td>Last problem</td>
                      <td className="muted">{status.data.health.last_error}</td>
                    </tr>
                  ) : null}
                </>
              ) : null}
            </tbody>
          </table>
        </div>
      ) : null}

      <div className="card">
        <h2 style={{ marginTop: 0 }}>Watch</h2>
        {instruments.data?.items.length ? (
          <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
            {instruments.data.items.map((instrument) => (
              <button
                key={instrument.id}
                className={selected.includes(instrument.id) ? "" : "secondary"}
                onClick={() => toggle(instrument.id)}
              >
                {instrument.symbol}
              </button>
            ))}
          </div>
        ) : (
          <p className="muted">
            No instruments are loaded. Use <em>Reload instruments</em> to import
            the provider&rsquo;s instrument file.
          </p>
        )}
      </div>

      {selected.length > 0 ? (
        <div className="card">
          <table>
            <thead>
              <tr>
                <th>Instrument</th>
                <th>Last</th>
                <th>Bid</th>
                <th>Ask</th>
                <th>Mid</th>
                <th>Volume</th>
                <th>OI</th>
                <th>Age</th>
                <th>Quality</th>
              </tr>
            </thead>
            <tbody>
              {(quotes.data?.items ?? []).map((quote) => (
                <QuoteRow key={quote.instrument_id} quote={quote} />
              ))}
            </tbody>
          </table>

          {quotes.data?.unavailable.length ? (
            <p className="muted">
              No live price is held for {quotes.data.unavailable.length}{" "}
              selected instrument
              {quotes.data.unavailable.length === 1 ? "" : "s"}. They are listed
              rather than dropped, so a short table is not mistaken for a
              complete one.
            </p>
          ) : null}
        </div>
      ) : null}
    </>
  );
}

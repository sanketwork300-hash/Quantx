"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import { Disclaimer, ErrorBanner } from "@/components/Ui";
import type {
  AccountPnl,
  AuditEvent,
  Envelope,
  GateCheck,
  Instrument,
  Order,
  SubmissionOutcome,
  TradingAccount,
} from "@/lib/types";

/** Flags that qualify what a fill rested on. Every paper fill carries
 * COUNTERFACTUAL, so it is shown once at the top rather than on every row. */
const QUALIFYING = new Set([
  "DEPTH_NOT_REPORTED",
  "LIMITED_BY_DEPTH",
  "LAST_TRADE_NOT_A_QUOTE",
  "COSTS_NOT_MODELLED",
]);

function money(value: string | null | undefined) {
  if (value === null || value === undefined) return "—";
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function StatusTag({ status }: { status: Order["status"] }) {
  const tone =
    status === "FILLED"
      ? "good"
      : status === "REJECTED" || status === "CANCELLED"
        ? "warn"
        : "info";
  return <span className={`tag ${tone}`}>{status.toLowerCase().replace(/_/g, " ")}</span>;
}

/**
 * The gate, shown whether or not it refused.
 *
 * An interface that displays its checks only on failure teaches people that
 * silence means safety, when it may only mean that no limit was configured.
 */
function Gate({ checks }: { checks: GateCheck[] }) {
  return (
    <table className="table compact">
      <thead>
        <tr>
          <th>check</th>
          <th>outcome</th>
          <th>detail</th>
        </tr>
      </thead>
      <tbody>
        {checks.map((check) => (
          <tr key={check.name}>
            <td>{check.name.replace(/_/g, " ")}</td>
            <td>
              {check.not_configured ? (
                <span className="tag">not configured</span>
              ) : check.passed ? (
                <span className="tag good">passed</span>
              ) : (
                <span className="tag bad">refused</span>
              )}
            </td>
            <td className="muted">{check.detail}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function TradingPage() {
  const queryClient = useQueryClient();
  const [accountId, setAccountId] = useState<string>("");
  const [instrumentId, setInstrumentId] = useState<string>("");
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [quantity, setQuantity] = useState("1");
  const [orderType, setOrderType] = useState<"MARKET" | "LIMIT">("MARKET");
  const [limitPrice, setLimitPrice] = useState("");
  const [newAccountName, setNewAccountName] = useState("");
  const [openingCash, setOpeningCash] = useState("1000000");
  const [killReason, setKillReason] = useState("");

  const accounts = useQuery({
    queryKey: ["trading-accounts"],
    queryFn: () => api.get<TradingAccount[]>("/trading/accounts"),
  });
  const instruments = useQuery({
    queryKey: ["trading-instruments"],
    queryFn: () => api.get<{ items: Instrument[] }>("/instruments?limit=200"),
  });

  const account = accounts.data?.find((item) => item.id === accountId);

  const orders = useQuery({
    queryKey: ["trading-orders", accountId],
    queryFn: () => api.get<Order[]>(`/trading/accounts/${accountId}/orders`),
    enabled: Boolean(accountId),
  });
  const pnl = useQuery({
    queryKey: ["trading-pnl", accountId],
    queryFn: () => api.get<Envelope<AccountPnl>>(`/trading/accounts/${accountId}/pnl`),
    enabled: Boolean(accountId),
  });
  const audit = useQuery({
    queryKey: ["trading-audit", accountId],
    queryFn: () => api.get<AuditEvent[]>(`/trading/accounts/${accountId}/audit`),
    enabled: Boolean(accountId),
  });

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["trading-orders", accountId] });
    queryClient.invalidateQueries({ queryKey: ["trading-pnl", accountId] });
    queryClient.invalidateQueries({ queryKey: ["trading-audit", accountId] });
    queryClient.invalidateQueries({ queryKey: ["trading-accounts"] });
  };

  const createAccount = useMutation({
    mutationFn: () =>
      api.post<TradingAccount>("/trading/accounts", {
        name: newAccountName,
        opening_cash: openingCash,
        venue: "PAPER",
      }),
    onSuccess: (created) => {
      setAccountId(created.id);
      setNewAccountName("");
      queryClient.invalidateQueries({ queryKey: ["trading-accounts"] });
    },
  });

  const submit = useMutation({
    mutationFn: () =>
      api.post<Envelope<SubmissionOutcome>>(`/trading/accounts/${accountId}/orders`, {
        instrument_id: instrumentId,
        side,
        quantity,
        order_type: orderType,
        limit_price: orderType === "LIMIT" ? limitPrice : null,
      }),
    onSuccess: refresh,
  });

  const work = useMutation({
    mutationFn: () => api.post<Order[]>(`/trading/accounts/${accountId}/work`, {}),
    onSuccess: refresh,
  });

  const arm = useMutation({
    mutationFn: () =>
      api.post<{ armed: boolean; refusals: string[] }>(
        `/trading/accounts/${accountId}/arm`,
        {},
      ),
    onSuccess: refresh,
  });

  const disarm = useMutation({
    mutationFn: () => api.del<unknown>(`/trading/accounts/${accountId}/arm`),
    onSuccess: refresh,
  });

  const engageKill = useMutation({
    mutationFn: () =>
      api.post<TradingAccount>(`/trading/accounts/${accountId}/kill-switch`, {
        reason: killReason,
      }),
    onSuccess: () => {
      setKillReason("");
      refresh();
    },
  });

  const outcome = submit.data?.results;
  const book = pnl.data?.results;

  return (
    <main className="page">
      <header className="page-header">
        <h1>Paper trading</h1>
        <p className="muted">
          Orders are filled against observed quotes and marked as counterfactual. No
          counterparty saw them, and nothing here is an instruction to trade.
        </p>
      </header>

      <section className="card">
        <h2>Account</h2>
        <div className="row">
          <select value={accountId} onChange={(event) => setAccountId(event.target.value)}>
            <option value="">select an account…</option>
            {(accounts.data ?? []).map((item) => (
              <option key={item.id} value={item.id}>
                {item.name} · {item.venue} · cash {money(item.cash)}
              </option>
            ))}
          </select>
          <input
            placeholder="new paper account name"
            value={newAccountName}
            onChange={(event) => setNewAccountName(event.target.value)}
          />
          <input
            placeholder="opening cash"
            value={openingCash}
            onChange={(event) => setOpeningCash(event.target.value)}
          />
          <button
            onClick={() => createAccount.mutate()}
            disabled={!newAccountName || createAccount.isPending}
          >
            Open
          </button>
        </div>
        {createAccount.error ? <ErrorBanner error={createAccount.error} /> : null}

        {account ? (
          <div className="notes">
            {account.kill_switch_engaged ? (
              <p className="tag bad">
                halted — {account.kill_switch_reason}. No order will be accepted until it
                is released.
              </p>
            ) : null}
            {!account.cost_schedule.models_costs ? (
              <p className="tag warn">
                no cost schedule: every P&amp;L figure on this account is gross of
                brokerage, exchange charges and statutory levies. Not a claim that trading
                is free — the rates are exchange rules the platform will not invent.
              </p>
            ) : null}
            {!account.risk_limits.any_set ? (
              <p className="tag warn">
                no risk limits are set, so only the checks that need none are run.
              </p>
            ) : null}
          </div>
        ) : null}
      </section>

      {accountId ? (
        <>
          <section className="card">
            <h2>Place an order</h2>
            <div className="row">
              <select
                value={instrumentId}
                onChange={(event) => setInstrumentId(event.target.value)}
              >
                <option value="">instrument…</option>
                {(instruments.data?.items ?? []).map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.symbol} · {item.exchange}
                  </option>
                ))}
              </select>
              <select value={side} onChange={(event) => setSide(event.target.value as "BUY")}>
                <option value="BUY">BUY</option>
                <option value="SELL">SELL</option>
              </select>
              <input
                value={quantity}
                onChange={(event) => setQuantity(event.target.value)}
                placeholder="quantity"
              />
              <select
                value={orderType}
                onChange={(event) => setOrderType(event.target.value as "MARKET")}
              >
                <option value="MARKET">MARKET</option>
                <option value="LIMIT">LIMIT</option>
              </select>
              {orderType === "LIMIT" ? (
                <input
                  value={limitPrice}
                  onChange={(event) => setLimitPrice(event.target.value)}
                  placeholder="limit price"
                />
              ) : null}
              <button
                onClick={() => submit.mutate()}
                disabled={!instrumentId || submit.isPending}
              >
                Submit
              </button>
              <button onClick={() => work.mutate()} disabled={work.isPending}>
                Work resting orders
              </button>
            </div>
            {submit.error ? <ErrorBanner error={submit.error} /> : null}

            {outcome ? (
              <div className="result">
                <h3>
                  <StatusTag status={outcome.order.status} />{" "}
                  {outcome.order.side} {outcome.order.quantity}
                  {outcome.replayed ? (
                    <span className="tag info">
                      already submitted — this client order id existed
                    </span>
                  ) : null}
                </h3>
                {outcome.order.rejection ? (
                  <p className="tag bad">
                    {outcome.order.rejection.reason}: {outcome.order.rejection.detail}
                  </p>
                ) : null}
                {outcome.order.fills.map((fill) => (
                  <p key={fill.id} className="muted">
                    {fill.quantity} at {money(fill.price)} ({fill.price_basis.toLowerCase()}),
                    decided against a quote stamped{" "}
                    {fill.quote_exchange_timestamp ?? "—"}
                    {fill.flags.filter((flag) => QUALIFYING.has(flag)).length > 0 ? (
                      <>
                        {" "}
                        <span className="tag warn">
                          {fill.flags
                            .filter((flag) => QUALIFYING.has(flag))
                            .join(", ")
                            .toLowerCase()
                            .replace(/_/g, " ")}
                        </span>
                      </>
                    ) : null}
                  </p>
                ))}
                <h4>Pre-trade checks</h4>
                <Gate checks={outcome.gate.checks} />
              </div>
            ) : null}
          </section>

          <section className="card">
            <h2>Book</h2>
            {book ? (
              <>
                <p className="muted">
                  cash {money(book.cash)} · realised {money(book.realised_pnl)} · unrealised{" "}
                  {book.unrealised_pnl === null ? (
                    <span className="tag warn">not reported: a position has no mark</span>
                  ) : (
                    money(book.unrealised_pnl)
                  )}{" "}
                  · equity{" "}
                  {book.equity === null ? (
                    <span className="tag warn">
                      withheld — {book.unpriced.length} position(s) cannot be valued
                    </span>
                  ) : (
                    money(book.equity)
                  )}
                  {book.gross_of_costs ? <span className="tag warn">gross of costs</span> : null}
                </p>
                <table className="table">
                  <thead>
                    <tr>
                      <th>instrument</th>
                      <th>quantity</th>
                      <th>average</th>
                      <th>mark</th>
                      <th>unrealised</th>
                      <th>realised</th>
                      <th>fees</th>
                    </tr>
                  </thead>
                  <tbody>
                    {book.positions.map((position) => (
                      <tr key={position.instrument_id}>
                        <td>{position.symbol ?? position.instrument_id}</td>
                        <td>{position.quantity}</td>
                        <td>{money(position.average_price)}</td>
                        <td>
                          {position.mark_price === null ? (
                            <span className="tag warn">no mark</span>
                          ) : (
                            <>
                              {money(position.mark_price)}{" "}
                              <span className="muted">({position.mark_basis})</span>
                            </>
                          )}
                        </td>
                        <td>{money(position.unrealised_pnl)}</td>
                        <td>{money(position.realised_pnl)}</td>
                        <td>{money(position.fees_paid)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            ) : (
              <p className="muted">no book yet</p>
            )}
          </section>

          <section className="card">
            <h2>Orders</h2>
            <table className="table">
              <thead>
                <tr>
                  <th>status</th>
                  <th>side</th>
                  <th>quantity</th>
                  <th>filled</th>
                  <th>average</th>
                  <th>fees</th>
                  <th>why</th>
                </tr>
              </thead>
              <tbody>
                {(orders.data ?? []).map((order) => (
                  <tr key={order.id}>
                    <td>
                      <StatusTag status={order.status} />
                    </td>
                    <td>{order.side}</td>
                    <td>{order.quantity}</td>
                    <td>{order.filled_quantity}</td>
                    <td>{money(order.average_fill_price)}</td>
                    <td>{money(order.fees)}</td>
                    <td className="muted">{order.rejection?.detail ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <section className="card">
            <h2>Live trading</h2>
            <p className="muted">
              Three separate gates guard a live order: this deployment must be
              configured to allow live trading, this account must be armed, and the
              broker adapter&rsquo;s mapping must have been confirmed against the
              broker&rsquo;s published contract. Arming reports every obstacle at once.
            </p>
            <div className="row">
              <span className={`tag ${account?.live_armed_at ? "good" : ""}`}>
                {account?.live_armed_at ? `armed ${account.live_armed_at}` : "not armed"}
              </span>
              <button onClick={() => arm.mutate()} disabled={arm.isPending}>
                Arm for live
              </button>
              <button onClick={() => disarm.mutate()} disabled={disarm.isPending}>
                Disarm
              </button>
            </div>
            {arm.data && !arm.data.armed ? (
              <ul className="notes">
                {arm.data.refusals.map((reason) => (
                  <li key={reason} className="tag warn">
                    {reason}
                  </li>
                ))}
              </ul>
            ) : null}
            {arm.error ? <ErrorBanner error={arm.error} /> : null}
          </section>

          <section className="card">
            <h2>Kill switch</h2>
            <p className="muted">
              Halts the account and cancels what is resting. A working order is exposure
              the switch is pulled to stop, so blocking new orders alone would not be it.
            </p>
            <div className="row">
              <input
                placeholder="reason — somebody will read this later"
                value={killReason}
                onChange={(event) => setKillReason(event.target.value)}
              />
              <button
                onClick={() => engageKill.mutate()}
                disabled={!killReason.trim() || engageKill.isPending}
              >
                Halt this account
              </button>
            </div>
            {engageKill.error ? <ErrorBanner error={engageKill.error} /> : null}
          </section>

          <section className="card">
            <h2>Audit trail</h2>
            <table className="table compact">
              <thead>
                <tr>
                  <th>when</th>
                  <th>event</th>
                  <th>transition</th>
                  <th>detail</th>
                </tr>
              </thead>
              <tbody>
                {(audit.data ?? []).slice(0, 50).map((event) => (
                  <tr key={event.id}>
                    <td>{event.occurred_at}</td>
                    <td>{event.event_type}</td>
                    <td>
                      {event.from_status ? `${event.from_status} → ${event.to_status}` : ""}
                    </td>
                    <td className="muted">{event.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </>
      ) : null}

      <Disclaimer />
    </main>
  );
}

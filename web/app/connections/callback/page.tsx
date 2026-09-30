"use client";

import { useMutation } from "@tanstack/react-query";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef } from "react";
import { api } from "@/lib/api";
import { ErrorBanner } from "@/components/Ui";
import { PENDING_PROVIDER_KEY } from "@/lib/connections";
import type { BrokerConnection, BrokerProvider } from "@/lib/types";

/**
 * Where the provider sends the browser back after the user approves.
 *
 * The authorization code arrives here in the URL and is forwarded to the API
 * with the caller's own bearer token; the exchange for an access token happens
 * server-side, so the credential itself never reaches the browser.
 */
function Callback() {
  const params = useSearchParams();
  const code = params.get("code");
  const state = params.get("state");
  const providerError = params.get("error");
  const submitted = useRef(false);

  const complete = useMutation({
    mutationFn: (args: { provider: BrokerProvider; code: string; state: string }) =>
      api.post<BrokerConnection>(`/connections/${args.provider}/callback`, {
        code: args.code,
        state: args.state,
      }),
  });

  useEffect(() => {
    // An authorization code is single-use; React must not send it twice.
    if (submitted.current || !code || !state) return;
    const provider = window.sessionStorage.getItem(
      PENDING_PROVIDER_KEY,
    ) as BrokerProvider | null;
    if (!provider) return;
    submitted.current = true;
    window.sessionStorage.removeItem(PENDING_PROVIDER_KEY);
    complete.mutate({ provider, code, state });
  }, [code, state, complete]);

  if (providerError) {
    return (
      <>
        <h1>Connection not completed</h1>
        <p className="subtitle">
          The provider returned <span className="mono">{providerError}</span>{" "}
          instead of an authorization code. Nothing was stored.
        </p>
        <Link href="/connections">Back to connections</Link>
      </>
    );
  }

  if (!code || !state) {
    return (
      <>
        <h1>Connection not completed</h1>
        <p className="subtitle">
          This page was opened without an authorization code, so there is
          nothing to exchange.
        </p>
        <Link href="/connections">Back to connections</Link>
      </>
    );
  }

  return (
    <>
      <h1>Finishing the connection</h1>
      <ErrorBanner error={complete.error} />
      {complete.isPending ? (
        <p className="subtitle">Exchanging the authorization code…</p>
      ) : null}
      {complete.isSuccess ? (
        <p className="subtitle">
          {complete.data.provider} is connected
          {complete.data.provider_account_id
            ? ` as ${complete.data.provider_account_id}`
            : ""}
          . The credential is stored encrypted and will be renewed without you
          being asked, for as long as the provider allows it.
        </p>
      ) : null}
      <Link href="/connections">Back to connections</Link>
    </>
  );
}

export default function CallbackPage() {
  return (
    <Suspense fallback={<p className="subtitle">Loading…</p>}>
      <Callback />
    </Suspense>
  );
}

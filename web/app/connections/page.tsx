"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { ErrorBanner } from "@/components/Ui";
import { PENDING_PROVIDER_KEY } from "@/lib/connections";
import type {
  AuthorizationHandoff,
  BrokerConnection,
  BrokerProvider,
  ConnectionList,
  ProviderList,
} from "@/lib/types";

const PROVIDER_LABELS: Record<BrokerProvider, string> = {
  upstox: "Upstox",
};

function StatusTag({ status }: { status: BrokerConnection["status"] }) {
  const tone =
    status === "CONNECTED" ? "good" : status === "REVOKED" ? "info" : "warn";
  const label =
    status === "NEEDS_REAUTHORIZATION" ? "needs sign-in" : status.toLowerCase();
  return <span className={`tag ${tone}`}>{label}</span>;
}

function formatWhen(value: string | null) {
  if (!value) return "—";
  return new Date(value).toLocaleString();
}

/** What the platform knows about when this credential stops working. */
function Expiry({ connection }: { connection: BrokerConnection }) {
  if (connection.expiry_source === "UNDECLARED") {
    return (
      <span className="muted">
        not stated by the provider
        {connection.status === "CONNECTED"
          ? " — used until it is refused"
          : ""}
      </span>
    );
  }
  return (
    <>
      {formatWhen(connection.expires_at)}{" "}
      <span className="muted">
        {connection.has_refresh_token
          ? "· renews automatically"
          : "· you will be asked to sign in again"}
      </span>
    </>
  );
}

export default function ConnectionsPage() {
  const queryClient = useQueryClient();

  const providers = useQuery({
    queryKey: ["connection-providers"],
    queryFn: () => api.get<ProviderList>("/connections/providers"),
  });

  const connections = useQuery({
    queryKey: ["connections"],
    queryFn: () => api.get<ConnectionList>("/connections"),
  });

  const connect = useMutation({
    mutationFn: async (provider: BrokerProvider) => {
      const handoff = await api.post<AuthorizationHandoff>(
        `/connections/${provider}/authorize`,
      );
      window.sessionStorage.setItem(PENDING_PROVIDER_KEY, provider);
      // The credential is granted at the broker, not here: this page never sees
      // the account password and never asks for a token.
      window.location.href = handoff.authorization_url;
      return handoff;
    },
  });

  const disconnect = useMutation({
    mutationFn: (provider: BrokerProvider) =>
      api.del<BrokerConnection>(`/connections/${provider}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["connections"] });
    },
  });

  const byProvider = new Map(
    (connections.data?.items ?? []).map((item) => [item.provider, item]),
  );

  return (
    <>
      <h2>Broker connections</h2>
      <p className="subtitle">
        Market data and trading credentials are granted through the provider’s
        own sign-in and stored encrypted against your account. Nothing here is
        pasted into a configuration file, and no access token is ever shown
        back to you.
      </p>

      <ErrorBanner error={connect.error ?? disconnect.error} />

      {providers.data && !providers.data.credential_storage_ready ? (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Credential storage is not set up</h3>
          <p className="muted">
            {providers.data.credential_storage_detail}
          </p>
          <p className="muted">
            Until an encryption key is configured the platform will not store a
            broker credential at all, rather than storing one it cannot protect.
          </p>
        </div>
      ) : null}

      {(providers.data?.items ?? []).map((provider) => {
        const connection = byProvider.get(provider.provider);
        const label = PROVIDER_LABELS[provider.provider] ?? provider.provider;
        return (
          <div className="card" key={provider.provider}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <h3 style={{ margin: 0 }}>
                {label}{" "}
                {connection ? <StatusTag status={connection.status} /> : null}
              </h3>
              <div className="row">
                {provider.configured ? (
                  <button
                    disabled={connect.isPending}
                    onClick={() => connect.mutate(provider.provider)}
                  >
                    {connection && connection.status === "CONNECTED"
                      ? "Reconnect"
                      : "Connect"}
                  </button>
                ) : null}
                {connection && connection.status !== "REVOKED" ? (
                  <button
                    className="secondary"
                    disabled={disconnect.isPending}
                    onClick={() => disconnect.mutate(provider.provider)}
                  >
                    Disconnect
                  </button>
                ) : null}
              </div>
            </div>

            {!provider.configured ? (
              <p className="muted">
                This deployment has no {label} app registration. An operator
                needs to set{" "}
                <span className="mono">
                  {provider.missing_settings.join(", ")}
                </span>
                .
              </p>
            ) : null}

            {connection ? (
              <table>
                <tbody>
                  <tr>
                    <td>Broker account</td>
                    <td>{connection.provider_account_id ?? "—"}</td>
                  </tr>
                  <tr>
                    <td>Connected</td>
                    <td>{formatWhen(connection.connected_at)}</td>
                  </tr>
                  <tr>
                    <td>Expires</td>
                    <td>
                      <Expiry connection={connection} />
                    </td>
                  </tr>
                  <tr>
                    <td>Last renewed</td>
                    <td>{formatWhen(connection.last_refreshed_at)}</td>
                  </tr>
                  <tr>
                    <td>Last used</td>
                    <td>{formatWhen(connection.last_used_at)}</td>
                  </tr>
                  {connection.scopes.length ? (
                    <tr>
                      <td>Permissions</td>
                      <td className="mono">{connection.scopes.join(" ")}</td>
                    </tr>
                  ) : null}
                  {connection.last_error ? (
                    <tr>
                      <td>Last problem</td>
                      <td className="muted">{connection.last_error}</td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            ) : (
              <p className="muted">Not connected.</p>
            )}
          </div>
        );
      })}
    </>
  );
}

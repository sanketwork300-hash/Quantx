"use client";

import { useMutation } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, readToken, writeToken } from "@/lib/api";
import { ErrorBanner } from "@/components/Ui";

interface TokenResponse {
  access_token: string;
  expires_in: number;
}

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [signedIn, setSignedIn] = useState(false);

  useEffect(() => setSignedIn(readToken() !== null), []);

  const login = useMutation({
    mutationFn: async (mode: "login" | "register") => {
      if (mode === "register") {
        await api.post("/auth/register", { email, password });
      }
      return api.post<TokenResponse>("/auth/login", { email, password });
    },
    onSuccess: (data) => {
      writeToken(data.access_token);
      setSignedIn(true);
      router.push("/data");
    },
  });

  if (signedIn) {
    return (
      <>
        <h2>Account</h2>
        <p className="subtitle">You are signed in.</p>
        <button
          className="secondary"
          onClick={() => {
            writeToken(null);
            setSignedIn(false);
          }}
        >
          Sign out
        </button>
      </>
    );
  }

  return (
    <>
      <h2>Sign in</h2>
      <p className="subtitle">
        Portfolios, uploads and jobs are scoped to your account.
      </p>

      <ErrorBanner error={login.error} />

      <form
        className="card"
        style={{ maxWidth: 420 }}
        onSubmit={(event) => {
          event.preventDefault();
          login.mutate("login");
        }}
      >
        <div className="field">
          <label htmlFor="email">Email</label>
          <input
            id="email"
            type="email"
            value={email}
            required
            style={{ width: "100%" }}
            onChange={(event) => setEmail(event.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="password">Password (at least 10 characters)</label>
          <input
            id="password"
            type="password"
            value={password}
            required
            minLength={10}
            style={{ width: "100%" }}
            onChange={(event) => setPassword(event.target.value)}
          />
        </div>
        <div className="row">
          <button type="submit" disabled={login.isPending}>
            {login.isPending ? "Working…" : "Sign in"}
          </button>
          <button
            type="button"
            className="secondary"
            disabled={login.isPending}
            onClick={() => login.mutate("register")}
          >
            Create account
          </button>
        </div>
      </form>
    </>
  );
}

"use client";

import { useState } from "react";

import { CSRF_HEADERS } from "@/lib/api";
import { formatApiError } from "@/lib/apiError";
import { useHydrated } from "@/lib/useHydrated";

export default function LoginForm({ next }: { next: string }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const hydrated = useHydrated();

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      // Not api(): a 401 here is a wrong password, not an ended session.
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { ...CSRF_HEADERS, "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        setError(formatApiError(body?.detail, "Couldn't sign in."));
        return;
      }
      // A full navigation, so the layout renders again with the session.
      window.location.assign(next);
    } catch {
      setError("Couldn't reach the server.");
    } finally {
      setBusy(false);
    }
  }

  const input = "input w-full";
  return (
    <div className="mx-auto mt-24 max-w-sm">
      <div className="mb-6 flex items-center gap-3">
        <span aria-hidden className="grid h-10 w-10 place-items-center rounded-lg bg-brand-400 font-bold text-brand-950">
          II
        </span>
        <div>
          <h1 className="text-xl font-semibold">Invoice Intelligence</h1>
          <p className="text-sm text-gray-500">Sign in to your account</p>
        </div>
      </div>
      <form onSubmit={submit} className="card overflow-hidden border-t-4 border-t-brand-400 p-6">
        {/* Disabled until interactive: see useHydrated. */}
        <fieldset disabled={!hydrated} className="space-y-4">
          <label className="block text-sm">
            <span className="mb-1 block text-gray-600">Email</span>
            <input
              type="email"
              autoComplete="username"
              required
              autoFocus
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className={input}
            />
          </label>
          <label className="block text-sm">
            <span className="mb-1 block text-gray-600">Password</span>
            <input
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className={input}
            />
          </label>
          {error && (
            <p role="alert" className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
              {error}
            </p>
          )}
          <button
            type="submit"
            disabled={busy}
            className="btn-primary w-full"
          >
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </fieldset>
      </form>
    </div>
  );
}

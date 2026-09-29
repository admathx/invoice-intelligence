"use client";

import { useState } from "react";

import AuthShell from "@/components/AuthShell";
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
    <AuthShell subtitle="Sign in to your account">
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
      {/* Outside the form's labels, so the field is still named "Password". */}
      <p className="mt-4 text-center text-sm">
        <a href="/forgot-password" className="link">
          Forgot your password?
        </a>
        <span aria-hidden className="mx-2 text-gray-300">
          ·
        </span>
        <a href="/help" className="link">
          Help
        </a>
      </p>
    </AuthShell>
  );
}

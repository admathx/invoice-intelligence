"use client";

import { useState } from "react";

import { CSRF_HEADERS } from "@/lib/api";
import { formatApiError } from "@/lib/apiError";

export default function LoginForm({ next }: { next: string }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

  const input = "w-full rounded border border-gray-300 px-3 py-2 text-sm";
  return (
    <div className="mx-auto mt-24 max-w-sm">
      <h1 className="mb-6 text-xl font-semibold">Invoice Intelligence</h1>
      <form onSubmit={submit} className="space-y-4 rounded border border-gray-200 bg-white p-6">
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
          <p role="alert" className="text-sm text-red-600">
            {error}
          </p>
        )}
        <button
          type="submit"
          disabled={busy}
          className="w-full rounded bg-gray-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}

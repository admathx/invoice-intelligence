"use client";

import { useState } from "react";

import { CSRF_HEADERS } from "@/lib/api";
import { formatApiError } from "@/lib/apiError";
import { useHydrated } from "@/lib/useHydrated";

/** Asks for a reset link (backend app/password_reset.py). The answer is the
 *  same whether or not the address has an account, so this page says the
 *  same thing either way too. */
export default function ForgotPasswordForm() {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sentTo, setSentTo] = useState<string | null>(null);
  const hydrated = useHydrated();

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/auth/password-reset/request", {
        method: "POST",
        headers: { ...CSRF_HEADERS, "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        setError(formatApiError(body?.detail, "Couldn't send a reset link."));
        return;
      }
      setSentTo(email.trim());
    } catch {
      setError("Couldn't reach the server.");
    } finally {
      setBusy(false);
    }
  }

  if (sentTo) {
    return (
      <div className="card border-t-4 border-t-brand-400 p-6 text-sm" role="status">
        <h2 className="section-title">Check your email</h2>
        <p className="mt-2 text-gray-700">
          If <strong className="[overflow-wrap:anywhere]">{sentTo}</strong> has an account, a link to choose a new
          password is on its way. It works once, for an hour.
        </p>
        <p className="mt-2 text-gray-500">Nothing after a few minutes? Check spam, or ask whoever runs your account.</p>
        <p className="mt-4">
          <a href="/login" className="link">
            Back to sign in
          </a>
        </p>
      </div>
    );
  }

  return (
    <>
      <form onSubmit={submit} className="card border-t-4 border-t-brand-400 p-6">
        {/* Disabled until interactive: see useHydrated. */}
        <fieldset disabled={!hydrated} className="space-y-4">
          <p className="text-sm text-gray-600">Enter the email you sign in with, and we&rsquo;ll send you a link.</p>
          <label className="block text-sm">
            <span className="mb-1 block text-gray-600">Email</span>
            <input
              type="email"
              autoComplete="username"
              required
              autoFocus
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="input w-full"
            />
          </label>
          {error && (
            <p role="alert" className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
              {error}
            </p>
          )}
          <button type="submit" disabled={busy} className="btn-primary w-full">
            {busy ? "Sending…" : "Send reset link"}
          </button>
        </fieldset>
      </form>
      <p className="mt-4 text-center text-sm">
        <a href="/login" className="link">
          Back to sign in
        </a>
      </p>
    </>
  );
}

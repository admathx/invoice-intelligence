"use client";

import { useState } from "react";

import { api, jsonInit } from "@/lib/api";
import { formatApiError } from "@/lib/apiError";
import NewPasswordFields, { newPasswordProblem } from "@/components/NewPasswordFields";
import { useHydrated } from "@/lib/useHydrated";


export default function ChangePasswordForm({ required, email }: { required: boolean; email: string }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const hydrated = useHydrated();

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const problem = newPasswordProblem(next, again);
    if (problem) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await api("/auth/password", jsonInit("POST", { current_password: current, new_password: next }));
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        setError(formatApiError(body?.detail, "Couldn't change the password."));
        return;
      }
      if (required) {
        // Everything was locked until now; a full load picks up the new state.
        window.location.assign("/invoices");
        return;
      }
      setDone(true);
      setCurrent("");
      setNext("");
      setAgain("");
    } catch {
      setError("Couldn't reach the server. Nothing was changed.");
    } finally {
      setBusy(false);
    }
  }

  const input = "input w-full";
  return (
    <div className="max-w-sm">
      {required ? (
        <h1 className="page-title">Choose your password</h1>
      ) : (
        <h2 className="section-title">Change your password</h2>
      )}
      <p className="mt-1 text-sm text-gray-600">
        {required
          ? "You signed in with a temporary password someone gave you. Choose one that only you know."
          : "You'll stay signed in here, and be signed out everywhere else."}
      </p>
      {done && (
        <p role="status" className="mt-4 rounded-lg border border-brand-200 border-l-4 border-l-brand-400 bg-brand-50 px-3 py-2 text-sm font-medium text-brand-900">
          Password changed.
        </p>
      )}
      <form onSubmit={submit} className="card mt-4 border-t-4 border-t-brand-300 p-5">
        {/* Disabled until interactive: see useHydrated. */}
        <fieldset disabled={!hydrated} className="space-y-3">
          {/* For password managers: which account this is. */}
          <input type="email" autoComplete="username" value={email} readOnly hidden />
          <label className="block text-sm">
            <span className="mb-1 block text-gray-600">{required ? "Temporary password" : "Current password"}</span>
            <input
              type="password"
              autoComplete="current-password"
              required
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
              className={input}
            />
          </label>
          <NewPasswordFields next={next} again={again} onNext={setNext} onAgain={setAgain} />
          {error && (
            <p className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700" data-testid="password-error">
              {error}
            </p>
          )}
          <button
            type="submit"
            disabled={busy}
            className="btn-primary w-full"
          >
            {busy ? "Saving…" : required ? "Set password and continue" : "Change password"}
          </button>
        </fieldset>
      </form>
    </div>
  );
}

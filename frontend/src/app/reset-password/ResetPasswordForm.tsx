"use client";

import { useEffect, useState } from "react";

import { CSRF_HEADERS } from "@/lib/api";
import { formatApiError } from "@/lib/apiError";
import { useHydrated } from "@/lib/useHydrated";

const MIN_LENGTH = 12; // backend app/users.py MIN_PASSWORD_LENGTH

async function post(path: string, body: unknown): Promise<Response> {
  return fetch(`/api/auth/${path}`, {
    method: "POST",
    headers: { ...CSRF_HEADERS, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

/** Sets a new password from an emailed link (backend app/password_reset.py),
 *  then signs in. */
const STORED = "ii_reset_token";

function forgetToken() {
  try {
    sessionStorage.removeItem(STORED);
  } catch {
    // nothing kept, nothing to clear
  }
}

/** Sets a new password from an emailed link (backend app/password_reset.py),
 *  then signs in. */
export default function ResetPasswordForm({ token: fromLink }: { token: string }) {
  const [token, setToken] = useState(fromLink);
  const [state, setState] = useState<"checking" | "ready" | "invalid">("checking");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const hydrated = useHydrated();

  useEffect(() => {
    // Out of the address bar (and the history, and anything shared from it):
    // until it's used, the token is as good as the password. Kept for this
    // tab only, so a reload still works.
    let found = fromLink;
    try {
      if (fromLink) sessionStorage.setItem(STORED, fromLink);
      else found = sessionStorage.getItem(STORED) ?? "";
    } catch {
      // Storage blocked: the link just won't survive a reload.
    }
    if (fromLink) window.history.replaceState(window.history.state, "", "/reset-password");
    setToken(found);
    if (!found) {
      setState("invalid");
      return;
    }
    post("password-reset/check", { token: found })
      .then((res) => {
        if (!res.ok) forgetToken();
        setState(res.ok ? "ready" : "invalid");
      })
      .catch(() => setState("ready")); // the submit will say what's wrong
  }, [fromLink]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (next !== again) {
      setError("the new passwords don't match");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await post("password-reset", { token, new_password: next });
      if (res.status === 410) {
        forgetToken();
        setState("invalid");
        return;
      }
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        setError(formatApiError(body?.detail, "Couldn't set the password."));
        return;
      }
      forgetToken();
      // Signed in now; a full load, so the layout renders with the session.
      window.location.assign("/invoices");
    } catch {
      setError("Couldn't reach the server. Nothing was changed.");
    } finally {
      setBusy(false);
    }
  }

  if (state === "invalid") {
    return (
      <div className="card border-t-4 border-t-amber-400 p-6 text-sm" role="alert">
        <h2 className="section-title">This link doesn&rsquo;t work any more</h2>
        <p className="mt-2 text-gray-700">
          Reset links work once, for an hour. Ask for a new one and use the most recent email.
        </p>
        <p className="mt-4">
          <a href="/forgot-password" className="btn-primary">
            Send a new link
          </a>
        </p>
      </div>
    );
  }

  const input = "input w-full";
  return (
    <form onSubmit={submit} className="card border-t-4 border-t-brand-400 p-6">
      {/* Disabled until interactive (see useHydrated) and until the link is known to work. */}
      <fieldset disabled={!hydrated || state === "checking"} className="space-y-4">
        <div className="text-sm">
          <label className="block">
            <span className="mb-1 block text-gray-600">New password</span>
            <input
              type="password"
              autoComplete="new-password"
              required
              autoFocus
              minLength={MIN_LENGTH}
              aria-describedby="reset-password-hint"
              value={next}
              onChange={(e) => setNext(e.target.value)}
              className={input}
            />
          </label>
          {/* Outside the label, so the field's name stays "New password". */}
          <span id="reset-password-hint" className="mt-1 block text-xs text-gray-500">
            At least {MIN_LENGTH} characters. A few unrelated words work well.
          </span>
        </div>
        <label className="block text-sm">
          <span className="mb-1 block text-gray-600">New password again</span>
          <input
            type="password"
            autoComplete="new-password"
            required
            value={again}
            onChange={(e) => setAgain(e.target.value)}
            className={input}
          />
        </label>
        {error && (
          <p role="alert" className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
            {error}
          </p>
        )}
        <button type="submit" disabled={busy} className="btn-primary w-full">
          {state === "checking" ? "Checking the link…" : busy ? "Saving…" : "Set password and sign in"}
        </button>
        <p className="text-xs text-gray-500">You&rsquo;ll be signed out everywhere else.</p>
      </fieldset>
    </form>
  );
}

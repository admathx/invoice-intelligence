"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { api } from "@/lib/api";
import { formatApiError } from "@/lib/apiError";

/** A small button that asks first, sends one request, then refreshes the
 *  page: dismissing an alert, sending a wrong match back to be matched. */
export default function ActionButton({
  label,
  question,
  path,
  className = "text-xs font-medium text-gray-600 hover:text-gray-900 hover:underline",
}: {
  label: string;
  question: string;
  path: string;
  className?: string;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    if (!window.confirm(question)) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api(path, { method: "POST" });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        setError(formatApiError(body?.detail, "That didn't work. Try again."));
        return;
      }
      router.refresh();
    } catch {
      setError("Couldn't reach the server. Nothing was changed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button type="button" onClick={() => void run()} disabled={busy} className={`${className} disabled:opacity-50`}>
        {busy ? "Working…" : label}
      </button>
      {error && (
        <span role="alert" className="ml-2 text-xs text-red-600">
          {error}
        </span>
      )}
    </>
  );
}

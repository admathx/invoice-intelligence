"use client";

import { useEffect } from "react";

import { reportError } from "@/lib/reportError";

/** When a page breaks: say so plainly, offer a way on, and let whoever runs
 *  the service know (they get an email; the person doesn't have to). */
export default function PageError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => reportError(error), [error]);
  return (
    <div className="max-w-md" role="alert">
      <h1 className="page-title">Something went wrong</h1>
      <p className="mt-2 text-sm text-gray-600">
        This page didn&rsquo;t load properly. We&rsquo;ve been told about it. Try again, and if it keeps happening, come
        back in a little while.
      </p>
      <p className="mt-4 flex flex-wrap gap-2">
        <button type="button" onClick={reset} className="btn-primary">
          Try again
        </button>
        <a href="/invoices" className="btn-secondary">
          Go to invoices
        </a>
      </p>
    </div>
  );
}

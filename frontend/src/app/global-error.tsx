"use client";

import { useEffect } from "react";

import { reportError } from "@/lib/reportError";

import "./globals.css";

/** When even the frame of the app breaks (app/error.tsx covers pages). It
 *  replaces the whole document, so it brings its own. */
export default function GlobalError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => reportError(error), [error]);
  return (
    <html lang="en">
      <body className="min-h-screen p-6">
        <div className="max-w-md" role="alert">
          <h1 className="page-title">Something went wrong</h1>
          <p className="mt-2 text-sm text-gray-600">
            The app didn&rsquo;t load properly. We&rsquo;ve been told about it. Try again in a moment.
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
      </body>
    </html>
  );
}

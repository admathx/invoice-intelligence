"use client";

import { useEffect, useState } from "react";

const FORMAT: Intl.DateTimeFormatOptions = { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" };

/** A moment, in the reader's own timezone.
 *
 * Formatting it where the page is rendered put it in the server's timezone:
 * right on a laptop, UTC in the production container, so a restaurant saw
 * an 8:40 AM correction as 3:40 PM. The server can't know the reader's zone,
 * so the first render says UTC explicitly, and the browser replaces it with
 * local time as soon as it runs (after hydration, so the two never disagree
 * mid-render). */
export default function LocalTime({ iso, className }: { iso: string; className?: string }) {
  const [local, setLocal] = useState<string | null>(null);
  useEffect(() => setLocal(new Date(iso).toLocaleString("en-US", FORMAT)), [iso]);
  const text = local ?? `${new Date(iso).toLocaleString("en-US", { ...FORMAT, timeZone: "UTC" })} UTC`;
  return (
    <time dateTime={iso} className={className} data-local={local !== null ? "" : undefined}>
      {text}
    </time>
  );
}

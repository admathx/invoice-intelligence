"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

/** While an invoice on the page is still being read, check again every few
 *  seconds, so it turns into its numbers without anyone reloading. Gives up
 *  after a while: one stuck that long is the scheduler's to pick up again
 *  (backend app/requeue.py), and a page left open shouldn't poll all day. */
export default function RefreshWhileReading({
  reading,
  everyMs = 5000,
  forMs = 20 * 60 * 1000,
}: {
  reading: boolean;
  everyMs?: number;
  forMs?: number;
}) {
  const router = useRouter();
  useEffect(() => {
    if (!reading) return;
    const started = Date.now();
    const timer = window.setInterval(() => {
      if (Date.now() - started > forMs) window.clearInterval(timer);
      else router.refresh();
    }, everyMs);
    return () => window.clearInterval(timer);
  }, [reading, everyMs, forMs, router]);
  return null;
}

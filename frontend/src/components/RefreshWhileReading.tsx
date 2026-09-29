"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

/** While an invoice on the page is still being read, check again every few
 *  seconds, so it turns into its numbers without anyone reloading. */
export default function RefreshWhileReading({ reading, everyMs = 5000 }: { reading: boolean; everyMs?: number }) {
  const router = useRouter();
  useEffect(() => {
    if (!reading) return;
    const timer = window.setInterval(() => router.refresh(), everyMs);
    return () => window.clearInterval(timer);
  }, [reading, everyMs, router]);
  return null;
}

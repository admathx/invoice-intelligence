"use client";

import { useEffect, useState } from "react";

/** False until the page is interactive (hydrated). Before then a form is
 *  just HTML: submitting it is a native GET to the same URL that resets the
 *  page, and anything typed is wiped when React takes over its controlled
 *  fields (then the browser's own "required" check silently blocks the
 *  submit). Forms stay disabled, fields included, until this is true. */
export function useHydrated(): boolean {
  const [hydrated, setHydrated] = useState(false);
  useEffect(() => setHydrated(true), []);
  return hydrated;
}

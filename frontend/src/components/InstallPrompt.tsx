"use client";

import { useEffect, useState } from "react";

type InstallEvent = Event & { prompt: () => Promise<void>; userChoice: Promise<{ outcome: string }> };

const HIDDEN_KEY = "ii_install_hidden";

/** On a phone, an offer to put the app on the home screen, so taking a photo
 *  of an invoice at the back door is two taps. Android's browsers can do it
 *  from a button; an iPhone only from the Share menu, so there it says how.
 *  Not shown once installed (it's then running as an app), or once hidden. */
export default function InstallPrompt() {
  const [install, setInstall] = useState<InstallEvent | null>(null);
  const [iphone, setIphone] = useState(false);
  const [hidden, setHidden] = useState(true);

  useEffect(() => {
    let dismissed = false;
    try {
      dismissed = localStorage.getItem(HIDDEN_KEY) === "1";
    } catch {
      // No storage: show it; hiding just won't be remembered.
    }
    const standalone =
      window.matchMedia("(display-mode: standalone)").matches ||
      (navigator as Navigator & { standalone?: boolean }).standalone === true;
    if (dismissed || standalone) return;
    setHidden(false);
    setIphone(/iPhone|iPad|iPod/.test(navigator.userAgent));
    const onPrompt = (e: Event) => {
      e.preventDefault(); // offered from our button instead of the browser's banner
      setInstall(e as InstallEvent);
    };
    window.addEventListener("beforeinstallprompt", onPrompt);
    return () => window.removeEventListener("beforeinstallprompt", onPrompt);
  }, []);

  function hide() {
    setHidden(true);
    try {
      localStorage.setItem(HIDDEN_KEY, "1");
    } catch {
      // Not remembered.
    }
  }

  if (hidden || (!install && !iphone)) return null;
  return (
    <div
      role="region"
      aria-label="Add to home screen"
      className="mb-4 flex items-center gap-3 rounded-lg border border-brand-200 bg-brand-50 px-3 py-2.5 text-sm text-brand-950 sm:hidden"
    >
      <span aria-hidden className="grid h-8 w-8 flex-none place-items-center rounded-md bg-brand-400 text-xs font-bold text-brand-950">
        II
      </span>
      <span className="min-w-0 flex-1">
        {install ? (
          "Put this on your home screen to snap invoices in two taps."
        ) : (
          <>
            Put this on your home screen: tap <strong>Share</strong>, then <strong>Add to Home Screen</strong>.
          </>
        )}
      </span>
      {install && (
        <button
          type="button"
          className="btn-primary btn-sm"
          onClick={() => {
            void install.prompt();
            void install.userChoice.then(() => setInstall(null));
          }}
        >
          Add
        </button>
      )}
      <button type="button" onClick={hide} aria-label="Hide" className="px-1 text-lg leading-none text-brand-800">
        ×
      </button>
    </div>
  );
}

import type { Metadata, Viewport } from "next";

import AccountMenu from "@/components/AccountMenu";
import NavLinks from "@/components/NavLinks";
import { SessionProvider } from "@/components/SessionContext";
import { getSession } from "@/lib/server";

import "./globals.css";

export const metadata: Metadata = {
  title: "Invoice Intelligence",
  description: "See what you pay for every product, catch price increases, and know what to ask your rep for.",
  // On an iPhone's home screen: opens full screen, named in a word.
  appleWebApp: { capable: true, title: "Invoices", statusBarStyle: "default" },
};

export const viewport: Viewport = {
  themeColor: "#4ade80",
};


export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const session = await getSession();

  return (
    <html lang="en">
      <body className="min-h-screen">
        {session && (
          // Sticky only where there's room: on a phone the header is most of
          // the screen, and pinning it hid the content under it.
          <header className="z-20 border-b border-gray-200 bg-white/95 backdrop-blur lg:sticky lg:top-0">
            {/* The accent, as a thin band across the top of every page. */}
            <div aria-hidden className="h-1 bg-gradient-to-r from-brand-300 via-brand-400 to-brand-300" />
            <nav className="flex flex-wrap items-center gap-x-4 gap-y-2 px-6 py-2.5 xl:flex-nowrap">
              <a
                href="/invoices"
                title="Invoice Intelligence"
                className="flex flex-none items-center gap-2 whitespace-nowrap font-semibold text-gray-900"
              >
                <span aria-hidden className="grid h-7 w-7 place-items-center rounded-md bg-brand-400 text-xs font-bold text-brand-950">
                  II
                </span>
                {/* The name where there's room for it and every link; the mark alone otherwise. */}
                <span className="md:hidden 2xl:inline">Invoice Intelligence</span>
              </a>
              <NavLinks operator={session.user.is_operator} />
              <AccountMenu
                name={session.user.name}
                locations={session.user.locations}
                locationId={session.locationId}
              />
            </nav>
          </header>
        )}
        <SessionProvider session={session}>
          <main className="p-6">{children}</main>
        </SessionProvider>
      </body>
    </html>
  );
}

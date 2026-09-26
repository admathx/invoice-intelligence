import type { Metadata } from "next";

import AccountMenu from "@/components/AccountMenu";
import { SessionProvider } from "@/components/SessionContext";
import { getSession } from "@/lib/server";

import "./globals.css";

export const metadata: Metadata = {
  title: "Invoice Intelligence",
  description: "Price creep, benchmarking, and negotiation sheets for restaurant invoices.",
};

const LINK = "whitespace-nowrap text-sm text-gray-600 hover:text-gray-900";

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const session = await getSession();

  return (
    <html lang="en">
      <body className="min-h-screen bg-gray-50 text-gray-900">
        {session && (
          <nav className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b bg-white px-6 py-3">
            <a href="/invoices" className="whitespace-nowrap font-semibold">
              Invoice Intelligence
            </a>
            <a href="/invoices" className={LINK}>
              Invoices
            </a>
            <a href="/skus" className={LINK}>
              SKUs
            </a>
            <a href="/review" className={LINK}>
              Review queue
            </a>
            <a href="/insights" className={LINK}>
              Insights
            </a>
            <a href="/negotiation" className={LINK}>
              Negotiation
            </a>
            <a href="/activity" className={LINK}>
              Activity
            </a>
            {/* Operators only: the one page that works across every
                location rather than inside the selected one. */}
            {session.user.is_operator && (
              <a href="/accounts" className="whitespace-nowrap text-sm text-gray-400 hover:text-gray-900">
                Businesses
              </a>
            )}
            <AccountMenu
              name={session.user.name}
              locations={session.user.locations}
              locationId={session.locationId}
            />
          </nav>
        )}
        <SessionProvider session={session}>
          <main className="p-6">{children}</main>
        </SessionProvider>
      </body>
    </html>
  );
}

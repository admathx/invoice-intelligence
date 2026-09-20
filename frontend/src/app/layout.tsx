import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Invoice Intelligence",
  description: "Price creep, benchmarking, and negotiation sheets for restaurant invoices.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-gray-50 text-gray-900">
        <nav className="border-b bg-white px-6 py-3 flex gap-4 items-center">
          <a href="/invoices" className="font-semibold">
            Invoice Intelligence
          </a>
          <a href="/invoices" className="text-sm text-gray-600 hover:text-gray-900">
            Invoices
          </a>
          <a href="/skus" className="text-sm text-gray-600 hover:text-gray-900">
            SKUs
          </a>
          <a href="/review" className="text-sm text-gray-600 hover:text-gray-900">
            Review queue
          </a>
          <a href="/insights" className="text-sm text-gray-600 hover:text-gray-900">
            Insights
          </a>
          <a href="/negotiation" className="text-sm text-gray-600 hover:text-gray-900">
            Negotiation
          </a>
          {/* Operator-facing, not tenant-facing: this is the only page that
              works across tenants rather than inside the one the dashboard is
              pointed at. Separated visually for that reason. */}
          <a href="/accounts" className="ml-auto text-sm text-gray-400 hover:text-gray-900">
            Businesses
          </a>
        </nav>
        <main className="p-6">{children}</main>
      </body>
    </html>
  );
}

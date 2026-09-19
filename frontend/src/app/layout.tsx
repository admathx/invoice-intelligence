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
        </nav>
        <main className="p-6">{children}</main>
      </body>
    </html>
  );
}

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
        <nav className="border-b bg-white px-6 py-3">
          <a href="/invoices" className="font-semibold">
            Invoice Intelligence
          </a>
        </nav>
        <main className="p-6">{children}</main>
      </body>
    </html>
  );
}

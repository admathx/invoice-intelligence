import NoLocation from "@/components/NoLocation";
import { requireSession, serverGet } from "@/lib/server";

import { StatusBadge } from "./[id]/InvoiceReview";
import UploadForm from "./UploadForm";

type Invoice = {
  id: string;
  invoice_number: string | null;
  invoice_date: string | null;
  total: string | null;
  status: string;
  source: string;
  created_at: string;
};

async function getInvoices(locationId: string): Promise<Invoice[]> {
  const res = await serverGet(`/invoices?tenant_id=${locationId}`);
  if (!res.ok) return [];
  return res.json();
}

export default async function InvoicesPage() {
  const { locationId } = await requireSession();
  if (!locationId) return <NoLocation />;
  const invoices = await getInvoices(locationId);

  // Surfaced up top because these invoices are otherwise invisible in the
  // numbers: their prices are held out of every benchmark, alert and
  // negotiation sheet until someone reviews them.
  const needsReview = invoices.filter((inv) => inv.status === "needs_review" || inv.status === "failed");

  return (
    <div>
      <h1 className="mb-4 text-xl font-semibold">Invoices</h1>
      {needsReview.length > 0 && (
        <div className="mb-4 rounded border border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-900">
          {needsReview.length} invoice{needsReview.length === 1 ? "" : "s"} couldn&rsquo;t be read or didn&rsquo;t add up, and{" "}
          {needsReview.length === 1 ? "is" : "are"} being kept out of your analytics until reviewed:{" "}
          {needsReview.slice(0, 5).map((inv, i) => (
            <span key={inv.id}>
              {i > 0 && ", "}
              <a href={`/invoices/${inv.id}`} className="font-medium underline">
                {inv.invoice_number ?? inv.id.slice(0, 8)}
              </a>
            </span>
          ))}
          {needsReview.length > 5 && ` and ${needsReview.length - 5} more`}
        </div>
      )}
      <UploadForm />
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b text-left text-gray-500">
            <th className="py-2 pr-4">Invoice #</th>
            <th className="py-2 pr-4">Date</th>
            <th className="py-2 pr-4">Total</th>
            <th className="py-2 pr-4">Status</th>
            <th className="py-2 pr-4">Source</th>
          </tr>
        </thead>
        <tbody>
          {invoices.map((inv) => (
            <tr key={inv.id} className="border-b">
              <td className="py-2 pr-4">
                <a href={`/invoices/${inv.id}`} className="text-blue-600 hover:underline">
                  {inv.invoice_number ?? inv.id.slice(0, 8)}
                </a>
              </td>
              <td className="py-2 pr-4">{inv.invoice_date ?? "—"}</td>
              <td className="py-2 pr-4">{inv.total ?? "—"}</td>
              <td className="py-2 pr-4">
                <StatusBadge status={inv.status} />
              </td>
              <td className="py-2 pr-4">{inv.source}</td>
            </tr>
          ))}
          {invoices.length === 0 && (
            <tr>
              <td colSpan={5} className="py-4 text-gray-500">
                No invoices yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

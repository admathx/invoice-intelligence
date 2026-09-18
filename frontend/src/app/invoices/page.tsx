import UploadForm from "./UploadForm";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";
const TENANT_ID = process.env.NEXT_PUBLIC_DEV_TENANT_ID ?? "";

type Invoice = {
  id: string;
  invoice_number: string | null;
  invoice_date: string | null;
  total: string | null;
  status: string;
  source: string;
  created_at: string;
};

async function getInvoices(): Promise<Invoice[]> {
  if (!TENANT_ID) return [];
  const res = await fetch(`${API_BASE}/invoices?tenant_id=${TENANT_ID}`, {
    cache: "no-store",
  });
  if (!res.ok) return [];
  return res.json();
}

export default async function InvoicesPage() {
  const invoices = await getInvoices();

  if (!TENANT_ID) {
    return (
      <p className="text-sm text-gray-600">
        Set <code>NEXT_PUBLIC_DEV_TENANT_ID</code> in <code>frontend/.env.local</code> to a tenant
        id (see <code>frontend/.env.local.example</code>).
      </p>
    );
  }

  return (
    <div>
      <h1 className="mb-4 text-xl font-semibold">Invoices</h1>
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
              <td className="py-2 pr-4">{inv.status}</td>
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

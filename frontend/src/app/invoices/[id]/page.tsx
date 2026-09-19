const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";
const TENANT_ID = process.env.NEXT_PUBLIC_DEV_TENANT_ID ?? "";

type LineItem = {
  id: string;
  line_number: number;
  raw_description: string;
  quantity: string;
  unit_price: string;
  extended_price: string;
  uom: string;
  review_status: string;
};

type InvoiceDetail = {
  id: string;
  invoice_number: string | null;
  status: string;
  subtotal: string | null;
  tax: string | null;
  total: string | null;
  line_items: LineItem[];
  page_image_urls: string[];
};

async function getInvoice(id: string): Promise<InvoiceDetail | null> {
  const res = await fetch(`${API_BASE}/invoices/${id}?tenant_id=${TENANT_ID}`, { cache: "no-store" });
  if (!res.ok) return null;
  return res.json();
}

export default async function InvoiceDetailPage({ params }: { params: { id: string } }) {
  const invoice = await getInvoice(params.id);

  if (!invoice) {
    return <p className="text-sm text-gray-600">Invoice not found.</p>;
  }

  return (
    <div>
      <h1 className="mb-1 text-xl font-semibold">{invoice.invoice_number ?? invoice.id}</h1>
      <p className="mb-4 text-sm text-gray-500">Status: {invoice.status}</p>

      <div className="flex gap-6">
        <div className="w-72 flex-none">
          {invoice.page_image_urls.length > 0 ? (
            <div className="space-y-3">
              {invoice.page_image_urls.map((url) => (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  key={url}
                  src={`${API_BASE}${url}`}
                  alt="Invoice page"
                  className="w-full rounded border border-gray-200"
                />
              ))}
            </div>
          ) : (
            <p className="text-sm text-gray-400">No page image available for this invoice.</p>
          )}
        </div>

        <div className="flex-1">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b text-left text-gray-500">
                <th className="py-2 pr-4">#</th>
                <th className="py-2 pr-4">Description</th>
                <th className="py-2 pr-4">Qty</th>
                <th className="py-2 pr-4">UOM</th>
                <th className="py-2 pr-4">Unit price</th>
                <th className="py-2 pr-4">Extended</th>
                <th className="py-2 pr-4">Review</th>
              </tr>
            </thead>
            <tbody>
              {invoice.line_items.map((li) => (
                <tr key={li.id} className="border-b">
                  <td className="py-2 pr-4">{li.line_number}</td>
                  <td className="py-2 pr-4">{li.raw_description}</td>
                  <td className="py-2 pr-4">{li.quantity}</td>
                  <td className="py-2 pr-4">{li.uom}</td>
                  <td className="py-2 pr-4">{li.unit_price}</td>
                  <td className="py-2 pr-4">{li.extended_price}</td>
                  <td className="py-2 pr-4">{li.review_status}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="mt-4 text-sm text-gray-600">
            Subtotal {invoice.subtotal} · Tax {invoice.tax} · Total {invoice.total}
          </div>
        </div>
      </div>
    </div>
  );
}

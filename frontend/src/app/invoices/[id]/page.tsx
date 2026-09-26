import InvoiceReview, { type Distributor, type InvoiceDetail } from "./InvoiceReview";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";
const TENANT_ID = process.env.NEXT_PUBLIC_DEV_TENANT_ID ?? "";

async function getInvoice(id: string): Promise<InvoiceDetail | null> {
  const res = await fetch(`${API_BASE}/invoices/${id}?tenant_id=${TENANT_ID}`, { cache: "no-store" });
  if (!res.ok) return null;
  return res.json();
}

async function getDistributors(): Promise<Distributor[]> {
  const res = await fetch(`${API_BASE}/distributors`, { cache: "no-store" });
  return res.ok ? res.json() : [];
}

export default async function InvoiceDetailPage({ params }: { params: { id: string } }) {
  const invoice = await getInvoice(params.id);
  if (!invoice) {
    return <p className="text-sm text-gray-600">Invoice not found.</p>;
  }
  // Only a reviewable invoice offers a distributor picker, so don't fetch the
  // list for every other invoice detail view.
  const distributors = invoice.status === "needs_review" ? await getDistributors() : [];
  return <InvoiceReview initial={invoice} distributors={distributors} />;
}

import ActivityList from "@/components/ActivityList";
import NoLocation from "@/components/NoLocation";
import type { AuditEvent } from "@/lib/activity";
import { requireSession, serverGet } from "@/lib/server";

import InvoiceReview, { type Distributor, type InvoiceDetail } from "./InvoiceReview";

async function getInvoice(id: string, locationId: string): Promise<InvoiceDetail | null> {
  const res = await serverGet(`/invoices/${id}?tenant_id=${locationId}`);
  if (!res.ok) return null;
  return res.json();
}

async function getHistory(id: string, locationId: string): Promise<AuditEvent[]> {
  const res = await serverGet(`/invoices/${id}/history?tenant_id=${locationId}`);
  return res.ok ? res.json() : [];
}

async function getDistributors(): Promise<Distributor[]> {
  const res = await serverGet("/distributors");
  return res.ok ? res.json() : [];
}

export default async function InvoiceDetailPage({ params }: { params: { id: string } }) {
  const { locationId } = await requireSession();
  if (!locationId) return <NoLocation />;
  const [invoice, history] = await Promise.all([getInvoice(params.id, locationId), getHistory(params.id, locationId)]);
  if (!invoice) {
    // Also what another location's invoice looks like: the API doesn't
    // confirm it exists. Worth a hint for someone with several locations.
    return (
      <p className="text-sm text-gray-600">
        Invoice not found at this location. If it belongs to another of your locations, switch to it above.
      </p>
    );
  }
  // Only a reviewable invoice offers a distributor picker, so don't fetch the
  // list for every other invoice detail view.
  const reviewable = invoice.status === "needs_review" || invoice.status === "failed";
  const distributors = reviewable ? await getDistributors() : [];
  return (
    <>
      <InvoiceReview initial={invoice} distributors={distributors} />
      <section className="mt-8 max-w-4xl">
        <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-gray-500">History</h2>
        <ActivityList events={history} />
      </section>
    </>
  );
}

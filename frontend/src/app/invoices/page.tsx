import InstallPrompt from "@/components/InstallPrompt";
import NoLocation from "@/components/NoLocation";
import RefreshWhileReading from "@/components/RefreshWhileReading";
import { money, READING_STATUSES, SOURCE_LABEL } from "@/lib/format";
import { requireSession, serverGet } from "@/lib/server";

import { StatusBadge } from "./[id]/InvoiceReview";
import ExportPanel from "./ExportPanel";
import SetupChecklist, { type SetupSteps } from "./SetupChecklist";
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

async function getSetup(locationId: string): Promise<SetupSteps | null> {
  const res = await serverGet(`/setup?tenant_id=${locationId}`);
  return res.ok ? res.json() : null;
}

async function getInvoices(locationId: string): Promise<Invoice[]> {
  const res = await serverGet(`/invoices?tenant_id=${locationId}`);
  if (!res.ok) return [];
  return res.json();
}

export default async function InvoicesPage() {
  const { user, locationId } = await requireSession();
  if (!locationId) return <NoLocation />;
  const [invoices, setup] = await Promise.all([getInvoices(locationId), getSetup(locationId)]);
  const location = user.locations.find((l) => l.id === locationId);
  const reading = invoices.filter((inv) => READING_STATUSES.has(inv.status)).length;

  // Surfaced up top because these invoices are otherwise invisible in the
  // numbers: their prices are held out of every benchmark, alert and
  // negotiation sheet until someone reviews them.
  const needsReview = invoices.filter((inv) => inv.status === "needs_review" || inv.status === "failed");

  const spend = invoices
    .filter((inv) => inv.status === "extracted" || inv.status === "confirmed")
    .reduce((sum, inv) => sum + Number(inv.total ?? 0), 0);

  return (
    <div className="max-w-6xl">
      {/* The export and photo panels open full width below the buttons
          (order-last), inside this same row. */}
      <RefreshWhileReading reading={reading > 0} />
      <InstallPrompt />
      <div className="mb-5 flex flex-wrap items-center gap-2">
        <div className="mr-auto min-w-0 pr-2">
          <h1 className="page-title">Invoices</h1>
          <p className="mt-0.5 text-sm text-gray-500">
            Upload an invoice or a photo of one
            {location?.inbox_address ? (
              <>
                , or email it to{" "}
                <span className="font-medium text-gray-700 [overflow-wrap:anywhere]">{location.inbox_address}</span>
              </>
            ) : null}
            .
          </p>
        </div>
        <ExportPanel />
        <UploadForm />
      </div>

      {setup && <SetupChecklist steps={setup} locationId={locationId} admin={user.is_operator} />}

      <div className="mb-5 grid gap-3 sm:grid-cols-3">
        <Stat label="Invoices" value={invoices.length.toLocaleString("en-US")} />
        <Stat label="Spend" value={money(spend)} tone="text-brand-700" />
        <Stat
          label="Need a look"
          value={needsReview.length.toLocaleString("en-US")}
          tone={needsReview.length ? "text-amber-600" : "text-gray-900"}
        />
      </div>

      {reading > 0 && (
        <p className="mb-3 text-sm text-sky-800" role="status">
          Reading {reading} invoice{reading === 1 ? "" : "s"}. This page updates by itself when {reading === 1 ? "it's" : "they're"} done.
        </p>
      )}

      {needsReview.length > 0 && (
        <div className="mb-5 rounded-lg border border-amber-200 border-l-4 border-l-amber-400 bg-amber-50 px-4 py-3 text-sm text-amber-900">
          {needsReview.length === 1 ? "1 invoice needs" : `${needsReview.length} invoices need`} a look. Until{" "}
          {needsReview.length === 1 ? "it's" : "they're"} fixed, {needsReview.length === 1 ? "its" : "their"} prices are
          left out of your alerts and savings:{" "}
          {needsReview.slice(0, 5).map((inv, i) => (
            <span key={inv.id}>
              {i > 0 && ", "}
              <a href={`/invoices/${inv.id}`} className="font-semibold underline">
                {inv.invoice_number ?? "no number"}
              </a>
            </span>
          ))}
          {needsReview.length > 5 && ` and ${needsReview.length - 5} more`}
        </div>
      )}

      <div className="card overflow-x-auto">
        <table className="w-full border-collapse text-sm">
          <thead className="bg-gray-50">
            <tr className="border-b border-gray-200">
              <th className="th pl-4">Invoice #</th>
              <th className="th">Date</th>
              <th className="th text-right">Total</th>
              <th className="th">Status</th>
              <th className="th">Source</th>
            </tr>
          </thead>
          <tbody>
            {invoices.map((inv) => (
              <tr key={inv.id} className="border-b border-gray-100 last:border-0 hover:bg-brand-50/40">
                <td className="whitespace-nowrap py-2.5 pl-4 pr-4">
                  <a href={`/invoices/${inv.id}`} className="link">
                    {inv.invoice_number ?? "No number"}
                  </a>
                </td>
                <td className="whitespace-nowrap py-2.5 pr-4 text-gray-600">{inv.invoice_date ?? "—"}</td>
                <td className="num whitespace-nowrap py-2.5 pr-4 text-right font-medium">{money(inv.total)}</td>
                <td className="py-2.5 pr-4">
                  <StatusBadge status={inv.status} />
                </td>
                <td className="py-2.5 pr-4 text-gray-600">{SOURCE_LABEL[inv.source] ?? inv.source}</td>
              </tr>
            ))}
            {invoices.length === 0 && (
              <tr>
                <td colSpan={5} className="px-4 py-6 text-gray-500">
                  No invoices yet. Use <strong>Upload invoice</strong> above to add your first one.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Stat({ label, value, tone = "text-gray-900" }: { label: string; value: string; tone?: string }) {
  return (
    <div className="card border-t-4 border-t-brand-300 px-4 py-3">
      <div className="text-xs font-semibold uppercase tracking-wide text-gray-500">{label}</div>
      <div className={`num mt-1 text-2xl font-semibold ${tone}`}>{value}</div>
    </div>
  );
}

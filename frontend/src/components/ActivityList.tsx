import { actorLabel, describeEvent, eventDetailLines, eventInvoiceId, type AuditEvent } from "@/lib/activity";

function when(iso: string): string {
  return new Date(iso).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** Who did what, newest first. `linkInvoices` adds a link to the invoice an
 *  event is about (pointless on that invoice's own page); `showLocation`
 *  names the location, for a list spanning several. */
export default function ActivityList({
  events,
  linkInvoices = false,
  showLocation = false,
}: {
  events: AuditEvent[];
  linkInvoices?: boolean;
  showLocation?: boolean;
}) {
  if (events.length === 0) return <p className="text-sm text-gray-500">Nothing recorded yet.</p>;
  return (
    <ol className="divide-y divide-gray-100 text-sm">
      {events.map((event) => {
        const invoiceId = linkInvoices ? eventInvoiceId(event) : null;
        const detail = eventDetailLines(event);
        return (
          <li key={event.id} className="py-2">
            <div className="flex gap-3">
              <time dateTime={event.occurred_at} className="w-32 flex-none text-gray-400">
                {when(event.occurred_at)}
              </time>
              <div>
                <span className="font-medium">{actorLabel(event)}</span> {describeEvent(event)}
                {showLocation && event.tenant_name && (
                  <span className="text-gray-400"> · {event.tenant_name}</span>
                )}
                {invoiceId && (
                  <>
                    {" · "}
                    <a href={`/invoices/${invoiceId}`} className="text-blue-600 hover:underline">
                      invoice
                    </a>
                  </>
                )}
                {detail.length > 0 && (
                  <ul className="mt-1 text-gray-600">
                    {detail.map((text, i) => (
                      // By position: two changes can read identically.
                      <li key={i}>{text}</li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

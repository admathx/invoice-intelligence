import LocalTime from "@/components/LocalTime";
import {
  actorLabel,
  describeEvent,
  EVENT_DOT,
  eventDetailLines,
  eventInvoiceId,
  eventKind,
  type AuditEvent,
} from "@/lib/activity";

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
  if (events.length === 0) return <p className="text-sm text-gray-500">Nothing has happened here yet.</p>;
  return (
    <ol className="card divide-y divide-gray-100 px-4 text-sm">
      {events.map((event) => {
        const invoiceId = linkInvoices ? eventInvoiceId(event) : null;
        const detail = eventDetailLines(event);
        return (
          <li key={event.id} className="py-2.5">
            <div className="flex gap-3">
              <span aria-hidden className={`mt-1.5 h-2 w-2 flex-none rounded-full ${EVENT_DOT[eventKind(event)]}`} />
              <LocalTime iso={event.occurred_at} className="num w-24 flex-none text-gray-400 sm:w-36" />
              {/* Events carry email and forwarding addresses, which have no
                  spaces to wrap at: allowed to break anywhere rather than
                  run off a phone screen. */}
              <div className="min-w-0 [overflow-wrap:anywhere]">
                <span className="font-medium">{actorLabel(event)}</span> {describeEvent(event)}
                {showLocation && event.tenant_name && (
                  <span className="text-gray-400"> · {event.tenant_name}</span>
                )}
                {invoiceId && (
                  <>
                    {" · "}
                    {/* ?location= opens it at its own location, even from the
                        change log, which spans all of them. */}
                    <a
                      href={`/invoices/${invoiceId}${event.tenant_id ? `?location=${event.tenant_id}` : ""}`}
                      className="link"
                    >
                      see invoice
                    </a>
                  </>
                )}
                {detail.length > 0 && (
                  <ul className="num mt-1 space-y-0.5 border-l-2 border-brand-200 pl-3 text-gray-600">
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

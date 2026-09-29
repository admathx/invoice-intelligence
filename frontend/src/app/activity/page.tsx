import ActivityList from "@/components/ActivityList";
import NoLocation from "@/components/NoLocation";
import type { AuditEvent } from "@/lib/activity";
import { requireSession, serverGet } from "@/lib/server";

export default async function ActivityPage() {
  const { user, locationId } = await requireSession();
  if (!locationId) return <NoLocation />;
  const res = await serverGet(`/activity?tenant_id=${locationId}&limit=200`);
  const events: AuditEvent[] = res.ok ? await res.json() : [];
  const location = user.locations.find((l) => l.id === locationId);

  return (
    <div className="max-w-4xl">
      <h1 className="page-title mb-1">Activity</h1>
      <p className="mb-4 text-sm text-gray-500">
        Everything that&rsquo;s happened at {location?.name ?? "this location"}, and who did it. Newest first.
      </p>
      <ActivityList events={events} linkInvoices />
    </div>
  );
}

import NoLocation from "@/components/NoLocation";
import { requireSession } from "@/lib/server";

import SpendingLoader from "./SpendingLoader";

export default async function SpendingPage() {
  const { locationId } = await requireSession();
  if (!locationId) return <NoLocation />;
  return (
    <div className="max-w-5xl">
      <h1 className="page-title">Spending</h1>
      <p className="mb-4 mt-0.5 text-sm text-gray-500">
        What you spend each month, before tax, by category and by distributor.
      </p>
      <SpendingLoader locationId={locationId} />
    </div>
  );
}

import NoLocation from "@/components/NoLocation";
import type { Costs } from "@/lib/costs";
import { requireSession, serverGet } from "@/lib/server";

import CostPlanner from "./CostPlanner";

async function getCosts(locationId: string): Promise<Costs | null> {
  const res = await serverGet(`/costs?tenant_id=${locationId}`);
  return res.ok ? res.json() : null;
}

export default async function CostsPage() {
  const { locationId } = await requireSession();
  if (!locationId) return <NoLocation />;
  const costs = await getCosts(locationId);

  return (
    <div className="max-w-5xl">
      <h1 className="page-title">Costs</h1>
      <p className="mb-4 mt-0.5 text-sm text-gray-500">
        What a year of your buying costs at today&rsquo;s prices, and what would change it.
      </p>
      {costs === null ? (
        <p role="alert" className="card px-4 py-4 text-sm text-red-700">
          We couldn&rsquo;t load your costs. Refresh the page to try again.
        </p>
      ) : costs.products.length === 0 ? (
        <div className="card border-l-4 border-l-brand-400 px-4 py-4 text-sm text-gray-700">
          Nothing to plan with yet. Once your invoices are read and their items{" "}
          <a href="/review" className="link">
            matched
          </a>
          , a year of your buying shows here.
        </div>
      ) : (
        <CostPlanner costs={costs} />
      )}
    </div>
  );
}

import NoLocation from "@/components/NoLocation";
import { requireSession, serverGet } from "@/lib/server";

import type { MonthSpend } from "./periods";
import SpendingView from "./SpendingView";

type Spending = { months: MonthSpend[]; not_counted: number };

export default async function SpendingPage() {
  const { locationId } = await requireSession();
  if (!locationId) return <NoLocation />;
  const res = await serverGet(`/spending?tenant_id=${locationId}`);
  const data: Spending | null = res.ok ? await res.json() : null;
  const anything = !!data?.months.some((m) => Number(m.total) > 0);

  return (
    <div className="max-w-5xl">
      <h1 className="page-title">Spending</h1>
      <p className="mb-4 mt-0.5 text-sm text-gray-500">
        What you spend each month, before tax, by category and by distributor.
      </p>
      {data && data.not_counted > 0 && (
        <p className="mb-4 rounded-lg border border-amber-200 border-l-4 border-l-amber-400 bg-amber-50 px-4 py-2.5 text-sm text-amber-900">
          {data.not_counted === 1 ? "1 invoice needs" : `${data.not_counted} invoices need`} a look and{" "}
          {data.not_counted === 1 ? "isn't" : "aren't"} counted here yet.{" "}
          <a href="/invoices" className="font-semibold underline">
            See invoices
          </a>
        </p>
      )}
      {!data ? (
        <p className="card px-4 py-4 text-sm text-gray-600">Couldn&rsquo;t load your spending. Try again in a moment.</p>
      ) : !anything ? (
        <p className="card px-4 py-4 text-sm text-gray-600">
          Nothing to show yet. Spending shows up here once your first invoices are read.{" "}
          <a href="/invoices" className="link">
            Add an invoice
          </a>
        </p>
      ) : (
        <SpendingView months={data.months} />
      )}
    </div>
  );
}

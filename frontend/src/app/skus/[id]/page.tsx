import ActionButton from "@/components/ActionButton";
import NoLocation from "@/components/NoLocation";
import { MATCH_LABEL, percent, priceChangeTone, REVIEW_BADGE, TONE_TEXT, unitPrice } from "@/lib/format";
import { requireSession, serverGet } from "@/lib/server";

type MatchedLine = {
  line_id: string;
  invoice_id: string;
  invoice_date: string | null;
  distributor_name: string;
  raw_description: string;
  normalized_unit_price: string | null;
  match_confidence: string | null;
  review_status: string;
};

type SkuDetail = {
  id: string;
  name: string;
  category: string;
  subcategory: string | null;
  base_uom: string;
  matched_lines: MatchedLine[];
};

async function getSku(id: string, locationId: string): Promise<SkuDetail | null> {
  const res = await serverGet(`/skus/${id}?tenant_id=${locationId}`);
  if (!res.ok) return null;
  return res.json();
}

export default async function SkuDetailPage({ params }: { params: { id: string } }) {
  const { locationId } = await requireSession();
  if (!locationId) return <NoLocation />;
  const sku = await getSku(params.id, locationId);

  if (!sku) {
    return (
      <p className="text-sm text-gray-600">
        We couldn&rsquo;t find that product.{" "}
        <a href="/skus" className="link">
          Search products
        </a>
      </p>
    );
  }

  const distributors = Array.from(new Set(sku.matched_lines.map((l) => l.distributor_name)));

  // Newest first; each price is compared with the delivery before it, so
  // the column reads as the price's history: up red, down green.
  const lines = sku.matched_lines;

  return (
    <div className="max-w-5xl">
      <a href="/skus" className="link mb-2 inline-block text-sm">
        &larr; Products
      </a>
      <h1 className="page-title">{sku.name}</h1>
      <p className="mt-1 flex flex-wrap items-center gap-2 text-sm text-gray-500">
        {sku.category}
        {sku.subcategory ? ` / ${sku.subcategory}` : ""}
        <span className="badge bg-gray-100 text-gray-600">priced per {sku.base_uom}</span>
      </p>
      <p className="mb-4 mt-2 text-sm text-gray-600">
        {distributors.length
          ? `Bought from ${distributors.length} distributor${distributors.length === 1 ? "" : "s"}: ${distributors.join(", ")}`
          : "Not bought here yet."}
      </p>

      <div className="card overflow-x-auto">
        <table className="w-full border-collapse text-sm">
          <thead className="bg-gray-50">
            <tr className="border-b border-gray-200">
              <th className="th pl-4">Date</th>
              <th className="th">Distributor</th>
              <th className="th">On the invoice</th>
              <th className="th text-right">Price per {sku.base_uom}</th>
              <th className="th text-right">How sure</th>
              <th className="th">Match</th>
            </tr>
          </thead>
          <tbody>
            {lines.map((l, i) => {
              const previous = lines[i + 1]?.normalized_unit_price;
              const change =
                l.normalized_unit_price && previous && Number(previous) !== 0
                  ? Number(l.normalized_unit_price) / Number(previous) - 1
                  : null;
              return (
                <tr key={i} className="border-b border-gray-100 last:border-0">
                  <td className="whitespace-nowrap py-2.5 pl-4 pr-4">
                    <a href={`/invoices/${l.invoice_id}`} className="link">
                      {l.invoice_date ?? "No date"}
                    </a>
                  </td>
                  <td className="whitespace-nowrap py-2.5 pr-4">{l.distributor_name}</td>
                  <td className="min-w-[10rem] py-2.5 pr-4">{l.raw_description}</td>
                  <td className="num whitespace-nowrap py-2.5 pr-4 text-right">
                    <span className="font-medium">{unitPrice(l.normalized_unit_price)}</span>
                    {change !== null && Math.abs(change) >= 0.0005 && (
                      <span className={`ml-2 text-xs font-semibold ${TONE_TEXT[priceChangeTone(change)]}`}>
                        {change > 0 ? "▲" : "▼"} {percent(Math.abs(change))}
                      </span>
                    )}
                  </td>
                  <td className="num py-2.5 pr-4 text-right text-gray-600">
                    {l.match_confidence === null ? "—" : `${Math.round(Number(l.match_confidence) * 100)}%`}
                  </td>
                  <td className="py-2.5 pr-4">
                    <span className={`badge ${REVIEW_BADGE[l.review_status] ?? "bg-gray-100 text-gray-700"}`}>
                      {MATCH_LABEL[l.review_status] ?? l.review_status}
                    </span>
                    {l.review_status !== "pending" && (
                      <div className="mt-1">
                        <ActionButton
                          label="Not this product?"
                          question={`Is “${l.raw_description}” not ${sku.name}? It goes back to Match items to be matched again, and its price stops counting here until it is.`}
                          path={`/review/${l.line_id}/reopen?tenant_id=${locationId}`}
                        />
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
            {lines.length === 0 && (
              <tr>
                <td colSpan={6} className="px-4 py-6 text-gray-500">
                  You haven&rsquo;t bought this product at this location yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

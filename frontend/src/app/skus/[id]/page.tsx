import NoLocation from "@/components/NoLocation";
import { requireSession, serverGet } from "@/lib/server";

type MatchedLine = {
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
    return <p className="text-sm text-gray-600">SKU not found.</p>;
  }

  const distributors = Array.from(new Set(sku.matched_lines.map((l) => l.distributor_name)));

  return (
    <div>
      <h1 className="mb-1 text-xl font-semibold">{sku.name}</h1>
      <p className="mb-4 text-sm text-gray-500">
        {sku.category}
        {sku.subcategory ? ` / ${sku.subcategory}` : ""} · price per {sku.base_uom}
      </p>
      <p className="mb-4 text-sm text-gray-600">
        Matched across {distributors.length} distributor{distributors.length === 1 ? "" : "s"}:{" "}
        {distributors.join(", ") || "none yet"}
      </p>

      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b text-left text-gray-500">
            <th className="py-2 pr-4">Date</th>
            <th className="py-2 pr-4">Distributor</th>
            <th className="py-2 pr-4">As printed</th>
            <th className="py-2 pr-4">Price / {sku.base_uom}</th>
            <th className="py-2 pr-4">Confidence</th>
            <th className="py-2 pr-4">Review</th>
          </tr>
        </thead>
        <tbody>
          {sku.matched_lines.map((l, i) => (
            <tr key={i} className="border-b">
              <td className="py-2 pr-4">{l.invoice_date ?? "—"}</td>
              <td className="py-2 pr-4">{l.distributor_name}</td>
              <td className="py-2 pr-4">{l.raw_description}</td>
              <td className="py-2 pr-4">{l.normalized_unit_price ?? "—"}</td>
              <td className="py-2 pr-4">{l.match_confidence ?? "—"}</td>
              <td className="py-2 pr-4">{l.review_status}</td>
            </tr>
          ))}
          {sku.matched_lines.length === 0 && (
            <tr>
              <td colSpan={6} className="py-4 text-gray-500">
                No matched invoice lines yet at this location.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

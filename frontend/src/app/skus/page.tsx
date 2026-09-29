"use client";

import { useState } from "react";

import { api } from "@/lib/api";

type SearchResult = {
  id: string;
  name: string;
  category: string;
  subcategory: string | null;
  base_uom: string;
};

export default function SkusPage() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [loading, setLoading] = useState(false);

  async function search(q: string) {
    setQuery(q);
    if (!q.trim()) {
      setResults([]);
      return;
    }
    setLoading(true);
    try {
      const res = await api(`/skus?q=${encodeURIComponent(q)}`);
      setResults(res.ok ? await res.json() : []);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="max-w-2xl">
      <h1 className="page-title">Products</h1>
      <p className="mb-4 mt-0.5 text-sm text-gray-500">Look up a product to see what you&rsquo;ve paid for it over time.</p>
      <input
        type="text"
        value={query}
        onChange={(e) => search(e.target.value)}
        aria-label="Search products"
        placeholder="Search products, e.g. mozzarella"
        className="input mb-4 w-full max-w-md"
      />
      {loading && <p className="text-sm text-gray-500">Searching…</p>}
      {(results.length > 0 || (query && !loading)) && (
        <ul className="card divide-y divide-gray-100">
          {results.map((r) => (
            <li key={r.id} className="flex items-center justify-between gap-3 px-4 py-2.5 hover:bg-brand-50/40">
              <a href={`/skus/${r.id}`} className="link">
                {r.name}
              </a>
              <span className="flex items-center gap-2 text-sm text-gray-500">
                {r.category}
                {r.subcategory ? ` / ${r.subcategory}` : ""}
                <span className="badge bg-gray-100 text-gray-600">priced per {r.base_uom}</span>
              </span>
            </li>
          ))}
          {query && !loading && results.length === 0 && <li className="px-4 py-3 text-sm text-gray-500">No products match. Try a shorter word.</li>}
        </ul>
      )}
    </div>
  );
}

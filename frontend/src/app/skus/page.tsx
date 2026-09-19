"use client";

import { useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

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
      const res = await fetch(`${API_BASE}/skus?q=${encodeURIComponent(q)}`);
      setResults(res.ok ? await res.json() : []);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div>
      <h1 className="mb-4 text-xl font-semibold">Canonical SKUs</h1>
      <input
        type="text"
        value={query}
        onChange={(e) => search(e.target.value)}
        placeholder="Search canonical SKUs (e.g. mozzarella)"
        className="mb-4 w-full max-w-md rounded border border-gray-300 px-3 py-2 text-sm"
      />
      {loading && <p className="text-sm text-gray-500">Searching...</p>}
      <ul className="divide-y divide-gray-200 max-w-2xl">
        {results.map((r) => (
          <li key={r.id} className="py-2">
            <a href={`/skus/${r.id}`} className="text-blue-600 hover:underline">
              {r.name}
            </a>
            <span className="ml-2 text-sm text-gray-500">
              {r.category}
              {r.subcategory ? ` / ${r.subcategory}` : ""} · {r.base_uom}
            </span>
          </li>
        ))}
        {query && !loading && results.length === 0 && (
          <li className="py-2 text-sm text-gray-500">No matches.</li>
        )}
      </ul>
    </div>
  );
}

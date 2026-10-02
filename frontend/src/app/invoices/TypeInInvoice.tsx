"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import DistributorPicker, { withDistributor, type Distributor } from "@/components/DistributorPicker";
import { useLocationId } from "@/components/SessionContext";
import { api, jsonInit } from "@/lib/api";
import { formatApiError } from "@/lib/apiError";
import { useHydrated } from "@/lib/useHydrated";

/** An invoice with no file to send: the paper is gone, or there never was
 *  any (a market run, a delivery only the distributor's website shows).
 *
 *  This asks only who it's from and when, which is what tells a copy from a
 *  new invoice. The items and the total are typed on the invoice's own
 *  screen, the one a misread invoice is fixed on, and checked the same way
 *  (backend app/api/invoices.py type_in_invoice). */
export default function TypeInInvoice() {
  const router = useRouter();
  const locationId = useLocationId();
  const hydrated = useHydrated();
  const [open, setOpen] = useState(false);
  const [distributors, setDistributors] = useState<Distributor[]>([]);
  const [distributorId, setDistributorId] = useState("");
  const [number, setNumber] = useState("");
  const [date, setDate] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || distributors.length || !locationId) return;
    api(`/distributors?tenant_id=${locationId}`)
      .then((res) => (res.ok ? res.json() : []))
      .then(setDistributors)
      .catch(() => setDistributors([]));
  }, [open, distributors.length, locationId]);

  async function start(e: React.FormEvent) {
    e.preventDefault();
    if (busy || !distributorId || !date) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api(
        `/invoices/typed?tenant_id=${locationId}`,
        jsonInit("POST", { distributor_id: distributorId, invoice_date: date, invoice_number: number.trim() || null }),
      );
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        setError(formatApiError(data?.detail, "Couldn't start that invoice."));
        setBusy(false);
        return;
      }
      // Stays busy while the invoice's screen loads, so it can't be sent twice.
      router.push(`/invoices/${data.id}`);
    } catch {
      setError("Couldn't reach the server. Nothing was added.");
      setBusy(false);
    }
  }

  return (
    <>
      <button
        type="button"
        className="btn-secondary"
        aria-expanded={open}
        disabled={!hydrated}
        onClick={() => setOpen((o) => !o)}
      >
        <span aria-hidden>✎</span> Type one in
      </button>

      {open && (
        <section
          aria-label="Type in an invoice"
          className="card order-last w-full border-l-4 border-l-brand-400 p-4"
          data-testid="type-in-panel"
        >
          <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
            <h2 className="section-title">Type in an invoice</h2>
            <p className="text-xs text-gray-500">
              For one you have no file or photo of. Its items and total go in on the next screen.
            </p>
          </div>

          <form onSubmit={start} className="mt-3">
            <div className="grid items-start gap-4 sm:grid-cols-3">
              <DistributorPicker
                stacked
                distributors={distributors}
                value={distributorId}
                onChange={setDistributorId}
                onAdded={(added) => setDistributors((list) => withDistributor(list, added))}
                addHint="If it's one already in the list, choose that instead."
                disabled={busy}
              />
              <label className="block min-w-0 text-sm">
                <span className="mb-1 block text-gray-600">Invoice date</span>
                <input
                  type="date"
                  required
                  value={date}
                  onChange={(e) => setDate(e.target.value)}
                  className="input w-full min-w-0"
                />
              </label>
              <label className="block min-w-0 text-sm">
                <span className="mb-1 block text-gray-600">
                  Invoice # <span className="text-gray-400">(if it has one)</span>
                </span>
                <input
                  value={number}
                  onChange={(e) => setNumber(e.target.value)}
                  maxLength={100}
                  className="input w-full min-w-0"
                />
              </label>
            </div>

            {error && (
              <p role="alert" className="mt-3 text-sm text-red-600">
                {error}
              </p>
            )}

            <div className="mt-4 flex flex-wrap items-center gap-2">
              <button type="submit" className="btn-primary" disabled={busy || !distributorId || !date}>
                {busy ? "Starting…" : "Continue to its items"}
              </button>
              <button type="button" className="btn-secondary" disabled={busy} onClick={() => setOpen(false)}>
                Cancel
              </button>
            </div>
          </form>
        </section>
      )}
    </>
  );
}

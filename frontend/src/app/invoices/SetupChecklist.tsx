"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";

export type SetupSteps = {
  first_invoice: boolean;
  emailed_invoice: boolean;
  items_matched: boolean;
  team_added: boolean;
  inbox_address: string | null;
  waiting_to_match: number;
};

type Step = { done: boolean; title: string; body: React.ReactNode };

/** A new location's first steps (backend app/api/setup.py). Each ticks
 *  itself off when it has happened; the card goes once all are done, or
 *  when this person hides it.
 *
 *  Fetches its own steps, and only while it can still be shown: once this
 *  browser knows the location is set up (or the card was hidden), the
 *  Invoices page stops asking on every visit. */
export default function SetupChecklist({ locationId, admin }: { locationId: string; admin: boolean }) {
  const hiddenKey = `ii_setup_hidden_${locationId}`;
  const doneKey = `ii_setup_done_${locationId}`;
  const [steps, setSteps] = useState<SetupSteps | null>(null);
  const [hidden, setHidden] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let settled = false;
    try {
      settled = localStorage.getItem(hiddenKey) === "1" || localStorage.getItem(doneKey) === "1";
    } catch {
      // No storage: ask each time, which is only a little wasteful.
    }
    if (settled) return;
    let cancelled = false;
    api(`/setup?tenant_id=${locationId}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data: SetupSteps | null) => {
        if (cancelled || !data) return;
        setSteps(data);
        if (data.first_invoice && data.emailed_invoice && data.items_matched && data.team_added) {
          try {
            localStorage.setItem(doneKey, "1");
          } catch {
            // Not remembered.
          }
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [locationId, hiddenKey, doneKey]);

  if (!steps || hidden) return null;

  const list: Step[] = [
    {
      done: steps.first_invoice,
      title: "Add your first invoice",
      body: (
        <>
          Press <strong>Upload invoice</strong> above and choose a PDF, or take a photo of a paper invoice.
        </>
      ),
    },
    {
      done: steps.emailed_invoice,
      title: "Have invoices emailed in",
      body: steps.inbox_address ? (
        <>
          Forward invoices, or ask your distributors to send them, to{" "}
          <span className="font-medium text-gray-900 [overflow-wrap:anywhere]">{steps.inbox_address}</span>{" "}
          <button
            type="button"
            className="link"
            onClick={() =>
              void navigator.clipboard?.writeText(steps.inbox_address!).then(
                () => setCopied(true),
                () => setCopied(false),
              )
            }
          >
            {copied ? "Copied" : "Copy"}
          </button>
        </>
      ) : (
        "This location doesn't have an invoice email yet. Ask whoever set up your account."
      ),
    },
    {
      done: steps.items_matched,
      title: "Match your items",
      body:
        steps.waiting_to_match > 0 ? (
          <>
            {steps.waiting_to_match} item{steps.waiting_to_match === 1 ? " is" : "s are"} waiting.{" "}
            <a href="/review" className="link">
              Match items
            </a>
          </>
        ) : (
          "Once invoices are read, say which product each item is. It takes a second each."
        ),
    },
    {
      done: steps.team_added,
      title: "Add your team",
      body: admin ? (
        <>
          Give everyone who handles invoices a login on the{" "}
          <a href="/users" className="link">
            People
          </a>{" "}
          page.
        </>
      ) : (
        "Ask your admin to add anyone else who handles invoices."
      ),
    },
  ];
  const doneCount = list.filter((s) => s.done).length;
  if (doneCount === list.length) return null;

  return (
    <section aria-labelledby="setup-title" className="card mb-5 border-l-4 border-l-brand-400 p-4" data-testid="setup-checklist">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="setup-title" className="section-title">
          Get set up{" "}
          <span className="badge bg-brand-100 text-brand-800">
            {doneCount} of {list.length} done
          </span>
        </h2>
        <button
          type="button"
          className="text-xs font-medium text-gray-500 hover:text-gray-800 hover:underline"
          onClick={() => {
            setHidden(true);
            try {
              localStorage.setItem(hiddenKey, "1");
            } catch {
              // Not remembered; it's back next visit, which is harmless.
            }
          }}
        >
          Hide
        </button>
      </div>
      <ol className="mt-3 grid gap-3 sm:grid-cols-2">
        {list.map((step, i) => (
          <li key={step.title} className="flex gap-3 text-sm">
            <span
              aria-hidden
              className={`mt-0.5 grid h-6 w-6 flex-none place-items-center rounded-full text-xs font-bold ${
                step.done ? "bg-brand-500 text-white" : "border-2 border-gray-300 text-gray-500"
              }`}
            >
              {step.done ? "✓" : i + 1}
            </span>
            <div className="min-w-0">
              <div className={`font-semibold ${step.done ? "text-gray-500 line-through decoration-gray-300" : "text-gray-900"}`}>
                {step.title}
                <span className="sr-only">{step.done ? " (done)" : " (to do)"}</span>
              </div>
              {!step.done && <div className="mt-0.5 text-gray-600">{step.body}</div>}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

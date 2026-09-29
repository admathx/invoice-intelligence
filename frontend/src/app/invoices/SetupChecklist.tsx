"use client";

import { useEffect, useState } from "react";

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
 *  when this person hides it (remembered in this browser only). */
export default function SetupChecklist({
  steps,
  locationId,
  admin,
}: {
  steps: SetupSteps;
  locationId: string;
  admin: boolean;
}) {
  const storageKey = `ii_setup_hidden_${locationId}`;
  // Hidden until known, so a hidden card doesn't flash on every load.
  const [hidden, setHidden] = useState(true);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    try {
      setHidden(localStorage.getItem(storageKey) === "1");
    } catch {
      setHidden(false);
    }
  }, [storageKey]);

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
  if (hidden || doneCount === list.length) return null;

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
              localStorage.setItem(storageKey, "1");
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

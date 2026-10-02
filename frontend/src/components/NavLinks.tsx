"use client";

import { usePathname } from "next/navigation";
import { useEffect, useRef } from "react";

type Item = { href: string; label: string };

// In the order of the work: invoices come in (and add up to spending), their
// items get matched to products, then price alerts and savings are worth
// reading, and costs puts a year of it together to plan with.
const MAIN: Item[] = [
  { href: "/invoices", label: "Invoices" },
  { href: "/spending", label: "Spending" },
  { href: "/review", label: "Match items" },
  { href: "/insights", label: "Price alerts" },
  { href: "/negotiation", label: "Savings" },
  { href: "/costs", label: "Costs" },
  { href: "/skus", label: "Products" },
  { href: "/activity", label: "Activity" },
];

// Operators only: the pages that work across every location rather than
// inside the selected one. Set apart after a divider for that reason.
const OPERATOR: Item[] = [
  { href: "/accounts", label: "Businesses" },
  { href: "/users", label: "People" },
  { href: "/audit", label: "Change log" },
];

function Link({ item, current }: { item: Item; current: boolean }) {
  return (
    <a
      href={item.href}
      aria-current={current ? "page" : undefined}
      className={`whitespace-nowrap rounded-md px-2 py-1.5 text-sm font-medium transition-colors ${
        current ? "bg-brand-100 text-brand-800" : "text-gray-600 hover:bg-gray-100 hover:text-gray-900"
      }`}
    >
      {item.label}
    </a>
  );
}

/** The section links, the current one highlighted in the accent. */
export default function NavLinks({ operator }: { operator: boolean }) {
  const pathname = usePathname();
  const isCurrent = (href: string) => pathname === href || pathname.startsWith(`${href}/`);
  const row = useRef<HTMLDivElement>(null);

  // On a phone the row scrolls; bring the current section into view rather
  // than leaving it off the edge.
  useEffect(() => {
    const current = row.current?.querySelector<HTMLElement>('[aria-current="page"]');
    if (current && row.current && row.current.scrollWidth > row.current.clientWidth) {
      row.current.scrollLeft = current.offsetLeft - (row.current.clientWidth - current.offsetWidth) / 2;
    }
  }, [pathname]);
  return (
    // One scrolling row on narrow screens, rather than wrapping into several.
    <div ref={row} data-nav-links className="-mx-2 flex min-w-0 max-w-full flex-auto items-center gap-1 overflow-x-auto px-2 md:mx-0 md:px-0">
      {MAIN.map((item) => (
        <Link key={item.href} item={item} current={isCurrent(item.href)} />
      ))}
      {operator && (
        <>
          <span aria-hidden className="mx-1 h-5 w-px bg-gray-200" />
          {OPERATOR.map((item) => (
            <Link key={item.href} item={item} current={isCurrent(item.href)} />
          ))}
        </>
      )}
    </div>
  );
}

"use client";

import { usePathname } from "next/navigation";

type Item = { href: string; label: string };

const MAIN: Item[] = [
  { href: "/invoices", label: "Invoices" },
  { href: "/review", label: "Review queue" },
  { href: "/insights", label: "Insights" },
  { href: "/negotiation", label: "Negotiation" },
  { href: "/skus", label: "SKUs" },
  { href: "/activity", label: "Activity" },
];

// Operators only: the pages that work across every location rather than
// inside the selected one. Set apart after a divider for that reason.
const OPERATOR: Item[] = [
  { href: "/accounts", label: "Businesses" },
  { href: "/users", label: "Users" },
  { href: "/audit", label: "Audit log" },
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
  return (
    // One scrolling row on narrow screens, rather than wrapping into several.
    <div data-nav-links className="-mx-2 flex min-w-0 max-w-full flex-auto items-center gap-1 overflow-x-auto px-2 md:mx-0 md:px-0">
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

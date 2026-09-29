/** A headline number: a label, the figure, and an optional line under it.
 *  The Invoices and Spending pages' summary cards. */
export default function StatTile({
  label,
  value,
  tone = "text-gray-900",
  children,
}: {
  label: string;
  value: string;
  tone?: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="card border-t-4 border-t-brand-300 px-4 py-3">
      <div className="text-xs font-semibold uppercase tracking-wide text-gray-500">{label}</div>
      <div className={`num mt-1 text-2xl font-semibold ${tone}`}>{value}</div>
      {children && <div className="mt-0.5 text-xs text-gray-500">{children}</div>}
    </div>
  );
}

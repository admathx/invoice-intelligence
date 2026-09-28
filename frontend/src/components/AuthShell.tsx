/** The frame of the signed-out pages (sign in, forgot password, reset
 *  password): the mark, a title, and a card under it. */
export default function AuthShell({
  subtitle,
  children,
}: {
  subtitle: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mx-auto mt-16 max-w-sm sm:mt-24">
      <div className="mb-6 flex items-center gap-3">
        <span aria-hidden className="grid h-10 w-10 flex-none place-items-center rounded-lg bg-brand-400 font-bold text-brand-950">
          II
        </span>
        <div className="min-w-0">
          <h1 className="text-xl font-semibold">Invoice Intelligence</h1>
          <p className="text-sm text-gray-500">{subtitle}</p>
        </div>
      </div>
      {children}
    </div>
  );
}

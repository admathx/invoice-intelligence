/** A link that leads nowhere: say so, and offer the way back. */
export default function NotFound() {
  return (
    <div className="max-w-md">
      <h1 className="page-title">Page not found</h1>
      <p className="mt-2 text-sm text-gray-600">That page doesn&rsquo;t exist. It may have moved, or the link was cut short.</p>
      <p className="mt-4 flex flex-wrap gap-2">
        <a href="/invoices" className="btn-primary">
          Go to invoices
        </a>
        <a href="/help" className="btn-secondary">
          Help
        </a>
      </p>
    </div>
  );
}

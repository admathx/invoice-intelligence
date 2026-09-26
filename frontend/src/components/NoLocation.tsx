/** Shown to a signed-in user who hasn't been given any location yet. */
export default function NoLocation() {
  return (
    <p className="text-sm text-gray-600">
      Your account doesn&rsquo;t have access to any locations yet. Ask whoever set up your login to add you to one.
    </p>
  );
}

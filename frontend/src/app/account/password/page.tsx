import { redirect } from "next/navigation";

import { getSession } from "@/lib/server";

import ChangePasswordForm from "./ChangePasswordForm";
import EmailPreferences from "./EmailPreferences";

export const metadata = { title: "Your account · Invoice Intelligence" };

// Not requireSession(): that sends anyone who must change their password
// here, and this page is where they do it.
export default async function ChangePasswordPage() {
  const session = await getSession();
  if (!session) redirect("/login");
  const { user } = session;
  if (user.password_change_required) {
    return <ChangePasswordForm required email={user.email} />;
  }
  return (
    <div className="space-y-8">
      <div>
        <h1 className="page-title">Your account</h1>
        <p className="mt-0.5 text-sm text-gray-500">
          Signed in as <span className="font-medium text-gray-700">{user.name}</span> ({user.email}).
        </p>
      </div>
      <ChangePasswordForm required={false} email={user.email} />
      <EmailPreferences
        initial={{ digest_enabled: user.digest_enabled, alert_emails_enabled: user.alert_emails_enabled }}
        alertThreshold={user.alert_email_min_pct_change}
      />
      {user.locations.length > 0 && (
        <section className="card max-w-lg p-5">
          <h2 className="section-title">Your locations</h2>
          <p className="mt-0.5 text-sm text-gray-500">
            Email or forward invoices to a location&rsquo;s address and they&rsquo;re added for you.
          </p>
          <ul className="mt-3 divide-y divide-gray-100 text-sm">
            {user.locations.map((l) => (
              <li key={l.id} className="py-2">
                <div className="font-medium">{l.name}</div>
                <div className="text-gray-600 [overflow-wrap:anywhere]">
                  {l.inbox_address ?? "No invoice email yet. Ask whoever set up your account."}
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

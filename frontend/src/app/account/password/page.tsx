import { redirect } from "next/navigation";

import { getSession } from "@/lib/server";

import ChangePasswordForm from "./ChangePasswordForm";
import EmailPreferences from "./EmailPreferences";

export const metadata = { title: "Change password · Invoice Intelligence" };

// Not requireSession(): that sends anyone who must change their password
// here, and this page is where they do it.
export default async function ChangePasswordPage() {
  const session = await getSession();
  if (!session) redirect("/login");
  const { user } = session;
  return (
    <div className="space-y-8">
      <ChangePasswordForm required={user.password_change_required} email={user.email} />
      {/* After the password is theirs: nothing else is reachable before that. */}
      {!user.password_change_required && <EmailPreferences initiallyEnabled={user.digest_enabled} />}
    </div>
  );
}

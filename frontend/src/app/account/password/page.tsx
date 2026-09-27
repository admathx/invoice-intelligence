import { redirect } from "next/navigation";

import { getSession } from "@/lib/server";

import ChangePasswordForm from "./ChangePasswordForm";

export const metadata = { title: "Change password · Invoice Intelligence" };

// Not requireSession(): that sends anyone who must change their password
// here, and this page is where they do it.
export default async function ChangePasswordPage() {
  const session = await getSession();
  if (!session) redirect("/login");
  return <ChangePasswordForm required={session.user.password_change_required} email={session.user.email} />;
}

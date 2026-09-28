import AuthShell from "@/components/AuthShell";

import ResetPasswordForm from "./ResetPasswordForm";

export const metadata = {
  title: "Choose a new password · Invoice Intelligence",
  // The link's token is in this page's address; never send it on.
  referrer: "no-referrer",
};

export default function ResetPasswordPage({ searchParams }: { searchParams: { token?: string } }) {
  return (
    <AuthShell subtitle="Choose a new password">
      <ResetPasswordForm token={searchParams.token ?? ""} />
    </AuthShell>
  );
}

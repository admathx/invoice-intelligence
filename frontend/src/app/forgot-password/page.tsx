import AuthShell from "@/components/AuthShell";

import ForgotPasswordForm from "./ForgotPasswordForm";

export const metadata = { title: "Forgot password · Invoice Intelligence" };

export default function ForgotPasswordPage() {
  return (
    <AuthShell subtitle="Reset your password">
      <ForgotPasswordForm />
    </AuthShell>
  );
}

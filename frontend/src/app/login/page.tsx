import { redirect } from "next/navigation";

import { safeNext } from "@/lib/api";
import { getSession } from "@/lib/server";

import LoginForm from "./LoginForm";

export const metadata = { title: "Sign in · Invoice Intelligence" };

export default async function LoginPage({ searchParams }: { searchParams: { next?: string } }) {
  const next = safeNext(searchParams.next);
  if (await getSession()) redirect(next);
  return <LoginForm next={next} />;
}

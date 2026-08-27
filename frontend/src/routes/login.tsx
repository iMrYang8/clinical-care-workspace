import { createFileRoute, redirect } from "@tanstack/react-router"

import { ClinicLoginForm } from "@/components/Auth/ClinicLoginForm"
import { AuthLayout } from "@/components/Common/AuthLayout"
import { roleHome, trustedSessionUser } from "@/hooks/useAuth"

export const Route = createFileRoute("/login")({
  beforeLoad: async () => {
    const user = await trustedSessionUser()
    if (user && user.role !== "worker") {
      throw redirect({ to: roleHome(user.role), replace: true })
    }
  },
  component: Login,
  head: () => ({
    meta: [{ title: "Clinical sign in · Nightingale" }],
  }),
})

function Login() {
  return (
    <AuthLayout>
      <ClinicLoginForm portal="clinical" />
    </AuthLayout>
  )
}

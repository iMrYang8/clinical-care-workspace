import { createFileRoute, redirect } from "@tanstack/react-router"
import { useState } from "react"

import { ClinicLoginForm } from "@/components/Auth/ClinicLoginForm"
import PatientOtpLoginForm from "@/components/Auth/PatientOtpLoginForm"
import { AuthLayout } from "@/components/Common/AuthLayout"
import { Button } from "@/components/ui/button"
import { roleHome, trustedSessionUser } from "@/hooks/useAuth"

export const Route = createFileRoute("/patient/login")({
  beforeLoad: async () => {
    const user = await trustedSessionUser()
    if (user && user.role !== "worker") {
      throw redirect({ to: roleHome(user.role), replace: true })
    }
  },
  component: PatientLogin,
  head: () => ({ meta: [{ title: "Patient sign in · Nightingale" }] }),
})

function PatientLogin() {
  const [legacyEmail, setLegacyEmail] = useState(false)
  return (
    <AuthLayout>
      {legacyEmail ? (
        <div className="space-y-5">
          <ClinicLoginForm portal="patient" />
          <Button
            className="w-full"
            onClick={() => setLegacyEmail(false)}
            variant="outline"
          >
            Use portal ID and phone code
          </Button>
        </div>
      ) : (
        <PatientOtpLoginForm onUseEmail={() => setLegacyEmail(true)} />
      )}
    </AuthLayout>
  )
}

import { useQuery } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { LoaderCircle } from "lucide-react"
import { useEffect } from "react"
import { SessionBoundaryError } from "@/components/Nightingale/SessionBoundaryError"
import { VoiceCapture } from "@/components/Voice/VoiceCapture"
import { patientSafeApi } from "@/features/api"
import useAuth, { roleHome } from "@/hooks/useAuth"

export const Route = createFileRoute("/_layout/my-care_/voice/capture")({
  component: PatientVoiceCaptureRoute,
  head: () => ({ meta: [{ title: "My voice recording · Nightingale" }] }),
})

function PatientVoiceCaptureRoute() {
  const navigate = useNavigate()
  const { user, meQuery, logout } = useAuth()
  const patients = useQuery({
    queryKey: ["patient-safe", "patients"],
    queryFn: patientSafeApi.patients,
    enabled: user?.role === "patient",
  })
  useEffect(() => {
    if (user && user.role !== "patient") {
      void navigate({ to: roleHome(user.role), replace: true })
    }
  }, [navigate, user])
  if (meQuery.isError || patients.isError) {
    return (
      <SessionBoundaryError
        error={meQuery.error ?? patients.error}
        onClear={logout}
      />
    )
  }
  if (user?.role !== "patient" || !patients.data?.[0]) {
    return (
      <LoaderCircle className="mx-auto mt-24 animate-spin text-amber-700" />
    )
  }
  return (
    <VoiceCapture
      patientId={patients.data[0].id}
      captureKind="patient"
      role={user.role}
      onFinalized={(sessionId) =>
        void navigate({
          to: "/my-care/voice/$sessionId",
          params: { sessionId },
        })
      }
    />
  )
}

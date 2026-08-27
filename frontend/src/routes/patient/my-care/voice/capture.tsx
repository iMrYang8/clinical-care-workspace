import { useQuery } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { LoaderCircle } from "lucide-react"

import { SessionBoundaryError } from "@/components/Nightingale/SessionBoundaryError"
import { VoiceCapture } from "@/components/Voice/VoiceCapture"
import { patientSafeApi } from "@/features/api"
import { recordingCodeFromSessionId } from "@/features/routeReferences"
import useAuth from "@/hooks/useAuth"

export const Route = createFileRoute("/patient/my-care/voice/capture")({
  component: PatientVoiceCaptureRoute,
  head: () => ({ meta: [{ title: "Record an update · Nightingale" }] }),
})

function PatientVoiceCaptureRoute() {
  const navigate = useNavigate()
  const { user, meQuery, logout } = useAuth()
  const patients = useQuery({
    queryKey: ["patient-safe", "patients"],
    queryFn: patientSafeApi.patients,
    enabled: user?.role === "patient",
  })

  if (meQuery.isError || patients.isError) {
    return (
      <SessionBoundaryError
        error={meQuery.error ?? patients.error}
        onClear={logout}
      />
    )
  }
  if (user?.role !== "patient" || !patients.data?.[0]) {
    return <LoaderCircle className="mx-auto mt-24 animate-spin text-primary" />
  }
  return (
    <VoiceCapture
      patientId={patients.data[0].id}
      captureKind="patient"
      role={user.role}
      owner={{
        userId: user.user_id,
        membershipId: user.membership_id,
        clinicId: user.clinic_id,
      }}
      onFinalized={(sessionId) =>
        void navigate({
          to: "/patient/my-care/voice/$sessionId",
          params: { sessionId: recordingCodeFromSessionId(sessionId) },
        })
      }
    />
  )
}

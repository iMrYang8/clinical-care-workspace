import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { LoaderCircle } from "lucide-react"
import { useEffect } from "react"
import { SessionBoundaryError } from "@/components/Nightingale/SessionBoundaryError"
import { VoiceCapture } from "@/components/Voice/VoiceCapture"
import useAuth, { roleHome } from "@/hooks/useAuth"

export const Route = createFileRoute(
  "/_layout/patients_/$patientId/voice/capture",
)({
  component: ClinicalVoiceCaptureRoute,
  head: () => ({ meta: [{ title: "Clinical voice capture · Nightingale" }] }),
})

function ClinicalVoiceCaptureRoute() {
  const { patientId } = Route.useParams()
  const navigate = useNavigate()
  const { user, meQuery, logout } = useAuth()
  const allowed = user?.role === "staff" || user?.role === "clinician"
  useEffect(() => {
    if (user && !allowed)
      void navigate({ to: roleHome(user.role), replace: true })
  }, [allowed, navigate, user])
  if (meQuery.isError) {
    return <SessionBoundaryError error={meQuery.error} onClear={logout} />
  }
  if (!user || !allowed) {
    return <LoaderCircle className="mx-auto mt-24 animate-spin text-teal-700" />
  }
  return (
    <VoiceCapture
      patientId={patientId}
      captureKind="clinical"
      role={user.role}
      onFinalized={(sessionId) =>
        void navigate({
          to: "/voice/$sessionId/review",
          params: { sessionId },
        })
      }
    />
  )
}

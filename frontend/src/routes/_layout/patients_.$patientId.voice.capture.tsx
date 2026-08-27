import {
  createFileRoute,
  notFound,
  redirect,
  useNavigate,
} from "@tanstack/react-router"
import { LoaderCircle } from "lucide-react"
import { useEffect } from "react"
import { SessionBoundaryError } from "@/components/Nightingale/SessionBoundaryError"
import { VoiceCapture } from "@/components/Voice/VoiceCapture"
import {
  recordingCodeFromSessionId,
  resolvePatientRouteReference,
} from "@/features/routeReferences"
import useAuth, { roleHome } from "@/hooks/useAuth"

export const Route = createFileRoute(
  "/_layout/patients_/$patientId/voice/capture",
)({
  beforeLoad: ({ params }) => {
    const patient = resolvePatientRouteReference(params.patientId)
    if (!patient) throw notFound()
    if (params.patientId !== patient.reference) {
      throw redirect({
        to: "/patients/$patientId/voice/capture",
        params: { patientId: patient.reference },
        replace: true,
      })
    }
    return { patientId: patient.id }
  },
  component: ClinicalVoiceCaptureRoute,
  head: () => ({ meta: [{ title: "Clinical voice capture · Nightingale" }] }),
})

function ClinicalVoiceCaptureRoute() {
  const { patientId } = Route.useRouteContext()
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
    return <LoaderCircle className="mx-auto mt-24 animate-spin text-primary" />
  }
  return (
    <VoiceCapture
      patientId={patientId}
      captureKind="clinical"
      role={user.role}
      owner={{
        userId: user.user_id,
        membershipId: user.membership_id,
        clinicId: user.clinic_id,
      }}
      onFinalized={(sessionId) =>
        void navigate({
          to: "/voice/$sessionId/review",
          params: { sessionId: recordingCodeFromSessionId(sessionId) },
        })
      }
    />
  )
}

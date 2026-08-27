import {
  createFileRoute,
  notFound,
  redirect,
  useNavigate,
} from "@tanstack/react-router"
import { LoaderCircle } from "lucide-react"
import { useEffect } from "react"
import { SessionBoundaryError } from "@/components/Nightingale/SessionBoundaryError"
import { VoiceReviewMode } from "@/components/Voice/VoiceReviewMode"
import { resolveRecordingRouteReference } from "@/features/routeReferences"
import useAuth, { roleHome } from "@/hooks/useAuth"

export const Route = createFileRoute("/_layout/voice/$sessionId/review")({
  beforeLoad: ({ params }) => {
    const recording = resolveRecordingRouteReference(params.sessionId)
    if (!recording) throw notFound()
    if (params.sessionId !== recording.reference) {
      throw redirect({
        to: "/voice/$sessionId/review",
        params: { sessionId: recording.reference },
        replace: true,
      })
    }
    return { sessionId: recording.id }
  },
  component: ClinicalVoiceReviewRoute,
  head: () => ({ meta: [{ title: "Voice review · Nightingale" }] }),
})

function ClinicalVoiceReviewRoute() {
  const { sessionId } = Route.useRouteContext()
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
  return <VoiceReviewMode sessionId={sessionId} membershipRole={user.role} />
}

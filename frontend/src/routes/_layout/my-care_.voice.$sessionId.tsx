import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { LoaderCircle } from "lucide-react"
import { useEffect } from "react"
import { SessionBoundaryError } from "@/components/Nightingale/SessionBoundaryError"
import { PatientVoiceStatus } from "@/components/Voice/PatientVoiceStatus"
import useAuth, { roleHome } from "@/hooks/useAuth"

export const Route = createFileRoute("/_layout/my-care_/voice/$sessionId")({
  component: PatientVoiceStatusRoute,
  head: () => ({ meta: [{ title: "My recording status · Nightingale" }] }),
})

function PatientVoiceStatusRoute() {
  const { sessionId } = Route.useParams()
  const navigate = useNavigate()
  const { user, meQuery, logout } = useAuth()
  useEffect(() => {
    if (user && user.role !== "patient") {
      void navigate({ to: roleHome(user.role), replace: true })
    }
  }, [navigate, user])
  if (meQuery.isError) {
    return <SessionBoundaryError error={meQuery.error} onClear={logout} />
  }
  if (user?.role !== "patient") {
    return (
      <LoaderCircle className="mx-auto mt-24 animate-spin text-amber-700" />
    )
  }
  return <PatientVoiceStatus sessionId={sessionId} />
}

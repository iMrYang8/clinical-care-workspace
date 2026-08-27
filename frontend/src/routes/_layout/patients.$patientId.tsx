import {
  createFileRoute,
  notFound,
  redirect,
  useNavigate,
} from "@tanstack/react-router"
import { LoaderCircle } from "lucide-react"
import { useEffect } from "react"

import { ClinicalCareNote } from "@/components/CareNote/ClinicalCareNote"
import { SessionBoundaryError } from "@/components/Nightingale/SessionBoundaryError"
import { resolvePatientRouteReference } from "@/features/routeReferences"
import useAuth, { roleHome } from "@/hooks/useAuth"

export const Route = createFileRoute("/_layout/patients/$patientId")({
  beforeLoad: ({ params }) => {
    const patient = resolvePatientRouteReference(params.patientId)
    if (!patient) throw notFound()
    if (params.patientId !== patient.reference) {
      throw redirect({
        to: "/patients/$patientId",
        params: { patientId: patient.reference },
        replace: true,
      })
    }
    return { patientId: patient.id }
  },
  component: PatientCareNoteRoute,
  head: () => ({ meta: [{ title: "Clinical care note · Nightingale" }] }),
})

function PatientCareNoteRoute() {
  const { patientId } = Route.useRouteContext()
  const navigate = useNavigate()
  const { user, meQuery, logout } = useAuth()
  const allowed =
    user?.role === "staff" ||
    user?.role === "clinician" ||
    user?.role === "admin"

  useEffect(() => {
    if (user && !allowed)
      void navigate({ to: roleHome(user.role), replace: true })
  }, [allowed, navigate, user])

  if (meQuery.isError) {
    return <SessionBoundaryError error={meQuery.error} onClear={logout} />
  }
  if (meQuery.isLoading || !user || !allowed) {
    return <LoaderCircle className="mx-auto mt-24 animate-spin text-primary" />
  }
  return <ClinicalCareNote currentUser={user} patientId={patientId} />
}

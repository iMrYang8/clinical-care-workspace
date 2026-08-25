import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { LoaderCircle } from "lucide-react"
import { useEffect } from "react"

import { ClinicalCareNote } from "@/components/CareNote/ClinicalCareNote"
import useAuth, { roleHome } from "@/hooks/useAuth"

export const Route = createFileRoute("/_layout/patients/$patientId")({
  component: PatientCareNoteRoute,
  head: () => ({ meta: [{ title: "Clinical care note · Nightingale" }] }),
})

function PatientCareNoteRoute() {
  const { patientId } = Route.useParams()
  const navigate = useNavigate()
  const { user, meQuery } = useAuth()
  const allowed = user?.role === "staff" || user?.role === "clinician"

  useEffect(() => {
    if (user && !allowed)
      void navigate({ to: roleHome(user.role), replace: true })
  }, [allowed, navigate, user])

  if (meQuery.isLoading || !user || !allowed) {
    return <LoaderCircle className="mx-auto mt-24 animate-spin text-teal-700" />
  }
  return <ClinicalCareNote currentUser={user} patientId={patientId} />
}

import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { LoaderCircle } from "lucide-react"
import { useEffect } from "react"

import { PatientSafeCareNote } from "@/components/Patient/PatientSafeCareNote"
import useAuth, { roleHome } from "@/hooks/useAuth"

export const Route = createFileRoute("/_layout/my-care")({
  component: MyCareRoute,
  head: () => ({ meta: [{ title: "My Care · Nightingale" }] }),
})

function MyCareRoute() {
  const navigate = useNavigate()
  const { user, meQuery } = useAuth()

  useEffect(() => {
    if (user && user.role !== "patient") {
      void navigate({ to: roleHome(user.role), replace: true })
    }
  }, [navigate, user])

  if (meQuery.isLoading || !user || user.role !== "patient") {
    return (
      <LoaderCircle className="mx-auto mt-24 animate-spin text-amber-600" />
    )
  }
  return <PatientSafeCareNote />
}

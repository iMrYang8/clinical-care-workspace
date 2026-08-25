import { useQuery } from "@tanstack/react-query"
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router"
import {
  ArrowRight,
  ClipboardList,
  LoaderCircle,
  UsersRound,
} from "lucide-react"
import { useEffect } from "react"
import { SessionBoundaryError } from "@/components/Nightingale/SessionBoundaryError"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent } from "@/components/ui/card"
import { apiErrorMessage, clinicalApi } from "@/features/api"
import useAuth, { roleHome } from "@/hooks/useAuth"

export const Route = createFileRoute("/_layout/patients/")({
  component: PatientsIndex,
  head: () => ({ meta: [{ title: "Care notes · Nightingale" }] }),
})

function PatientsIndex() {
  const navigate = useNavigate()
  const { user, meQuery, logout } = useAuth()
  const allowed = user?.role === "staff" || user?.role === "clinician"
  const patientsQuery = useQuery({
    queryKey: ["patients"],
    queryFn: clinicalApi.patients,
    enabled: allowed,
  })

  useEffect(() => {
    if (user && !allowed)
      void navigate({ to: roleHome(user.role), replace: true })
  }, [allowed, navigate, user])

  if (meQuery.isError) {
    return <SessionBoundaryError error={meQuery.error} onClear={logout} />
  }
  if (meQuery.isLoading || !user || !allowed) {
    return <LoaderCircle className="mx-auto mt-24 animate-spin text-teal-700" />
  }

  return (
    <div className="space-y-6">
      <header className="flex flex-col justify-between gap-4 rounded-2xl border bg-white p-6 sm:flex-row sm:items-end">
        <div>
          <p className="text-xs font-bold uppercase tracking-[0.18em] text-teal-700">
            Clinic workspace
          </p>
          <h1 className="mt-1 font-serif text-4xl font-semibold tracking-tight text-slate-950">
            Clinical care notes
          </h1>
          <p className="mt-2 max-w-2xl leading-7 text-slate-600">
            Open a synthetic patient to review the precomputed Glance,
            longitudinal timeline, immutable sources, comments, and version
            history.
          </p>
        </div>
        <Badge className="w-fit bg-teal-100 text-teal-800">
          {user.role} · clinic scoped
        </Badge>
      </header>

      {patientsQuery.isLoading && (
        <p className="flex items-center gap-2 text-slate-500">
          <LoaderCircle className="animate-spin" /> Loading clinic patients…
        </p>
      )}
      {patientsQuery.isError && (
        <Alert className="border-red-200 bg-red-50 text-red-900">
          <AlertDescription>
            {apiErrorMessage(patientsQuery.error)}
          </AlertDescription>
        </Alert>
      )}
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {patientsQuery.data?.map((patient) => (
          <Link
            aria-label={`Open care note for ${patient.display_name}`}
            params={{ patientId: patient.id }}
            to="/patients/$patientId"
            key={patient.id}
          >
            <Card className="h-full border-slate-200 bg-white transition hover:-translate-y-0.5 hover:border-teal-300 hover:shadow-md">
              <CardContent className="flex h-full items-center gap-4 p-5">
                <span className="grid size-12 place-items-center rounded-2xl bg-teal-100 text-teal-800">
                  <ClipboardList />
                </span>
                <div className="min-w-0 flex-1">
                  <p className="truncate font-serif text-xl font-semibold text-slate-950">
                    {patient.display_name}
                  </p>
                  <p className="mt-1 text-sm text-slate-500">
                    Synthetic patient record
                  </p>
                </div>
                <ArrowRight className="text-slate-400" />
              </CardContent>
            </Card>
          </Link>
        ))}
      </div>
      {patientsQuery.data?.length === 0 && (
        <div className="rounded-2xl border border-dashed bg-white py-16 text-center">
          <UsersRound className="mx-auto mb-3 size-8 text-slate-400" />
          <p className="font-medium">No patients in this clinic</p>
        </div>
      )}
    </div>
  )
}

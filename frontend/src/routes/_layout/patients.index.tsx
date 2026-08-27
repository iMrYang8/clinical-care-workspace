import { useQuery } from "@tanstack/react-query"
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router"
import {
  ArrowRight,
  CalendarClock,
  CalendarDays,
  ClipboardList,
  History,
  LoaderCircle,
  Plus,
  Search,
  UsersRound,
} from "lucide-react"
import { useDeferredValue, useEffect, useState } from "react"
import { SessionBoundaryError } from "@/components/Nightingale/SessionBoundaryError"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import {
  apiErrorMessage,
  clinicalApi,
  type PatientDirectoryItem,
} from "@/features/api"
import { patientRouteReferenceFromId } from "@/features/routeReferences"
import useAuth, { roleHome } from "@/hooks/useAuth"
import { formatSingaporeDate, formatSingaporeDateTime } from "@/lib/dateTime"

const roleLabels = {
  staff: "Care staff",
  clinician: "Clinician",
  admin: "Clinic administrator",
} as const

const visitStatusLabels: Record<string, string> = {
  scheduled: "Scheduled",
  checked_in: "Checked in",
  in_progress: "In progress",
  completed: "Completed",
}

const visitTypeLabels: Record<string, string> = {
  acute_review: "Acute review",
  follow_up: "Follow-up",
  medication_review: "Medication review",
  clinic_visit: "Clinic visit",
}

export const Route = createFileRoute("/_layout/patients/")({
  component: PatientsIndex,
  head: () => ({ meta: [{ title: "Patients · Nightingale" }] }),
})

function PatientCard({
  patient,
  today,
}: {
  patient: PatientDirectoryItem
  today: boolean
}) {
  return (
    <Link
      aria-label={`Open care note for ${patient.display_name}`}
      params={{ patientId: patientRouteReferenceFromId(patient.id) }}
      to="/patients/$patientId"
    >
      <Card className="h-full border-border bg-card transition hover:-translate-y-0.5 hover:border-primary/50 hover:shadow-md">
        <CardContent className="flex h-full items-center gap-4 p-5">
          <span className="grid size-12 shrink-0 place-items-center rounded-2xl bg-primary/10 text-primary">
            {today ? <CalendarClock /> : <ClipboardList />}
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <p className="truncate font-serif text-xl font-semibold text-foreground">
                {patient.display_name}
              </p>
              {today && patient.today_visit_status && (
                <Badge className="bg-primary/10 text-primary">
                  {visitStatusLabels[patient.today_visit_status] ??
                    patient.today_visit_status}
                </Badge>
              )}
            </div>
            <p className="mt-1 text-sm text-muted-foreground">
              {patient.medical_record_number ?? "MRN pending"}
            </p>
            {today && patient.today_visit_at ? (
              <p className="mt-1 flex flex-wrap items-center gap-1 text-xs font-medium text-foreground">
                <CalendarClock className="size-3" />
                {formatSingaporeDateTime(patient.today_visit_at)}
                {patient.today_visit_type && (
                  <span className="text-muted-foreground">
                    ·{" "}
                    {visitTypeLabels[patient.today_visit_type] ??
                      patient.today_visit_type}
                  </span>
                )}
              </p>
            ) : (
              patient.date_of_birth && (
                <p className="mt-1 flex items-center gap-1 text-xs text-muted-foreground">
                  <CalendarDays className="size-3" /> DOB{" "}
                  {formatSingaporeDate(patient.date_of_birth)}
                </p>
              )
            )}
            {patient.same_name_count > 1 && (
              <Badge
                className="mt-2 border-warning/40 bg-warning-muted text-warning-muted-foreground"
                variant="outline"
              >
                {patient.same_name_count} records share this name · verify
                DOB/MRN
              </Badge>
            )}
          </div>
          <ArrowRight className="shrink-0 text-muted-foreground" />
        </CardContent>
      </Card>
    </Link>
  )
}

function PatientsIndex() {
  const navigate = useNavigate()
  const { user, meQuery, logout } = useAuth()
  const [search, setSearch] = useState("")
  const deferredSearch = useDeferredValue(search.trim())
  const [offset, setOffset] = useState(0)
  const pageSize = 24
  const allowed =
    user?.role === "staff" ||
    user?.role === "clinician" ||
    user?.role === "admin"
  const todayQuery = useQuery({
    queryKey: ["patients", "directory", "today", deferredSearch],
    queryFn: () =>
      clinicalApi.patientDirectory({
        search: deferredSearch || undefined,
        visitScope: "today",
        offset: 0,
        limit: 100,
      }),
    enabled: allowed,
  })
  const previousQuery = useQuery({
    queryKey: ["patients", "directory", "previous", deferredSearch, offset],
    queryFn: () =>
      clinicalApi.patientDirectory({
        search: deferredSearch || undefined,
        visitScope: "previous",
        offset,
        limit: pageSize,
      }),
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
    return <LoaderCircle className="mx-auto mt-24 animate-spin text-primary" />
  }

  const totalMatches =
    (todayQuery.data?.count ?? 0) + (previousQuery.data?.count ?? 0)
  const queryError = todayQuery.error ?? previousQuery.error

  return (
    <div className="space-y-6">
      <header className="flex flex-col justify-between gap-4 rounded-2xl border bg-card p-6 sm:flex-row sm:items-end">
        <div>
          <p className="text-xs font-bold uppercase tracking-[0.18em] text-primary">
            Clinic workspace
          </p>
          <h1 className="mt-1 font-serif text-4xl font-semibold tracking-tight text-foreground">
            Patients
          </h1>
          <p className="mt-2 max-w-2xl leading-7 text-muted-foreground">
            Start with today&apos;s visits, or search the clinic&apos;s previous
            patient records.
          </p>
        </div>
        <Badge className="w-fit bg-primary/10 text-primary">
          {roleLabels[user.role as keyof typeof roleLabels] ??
            "Clinic team member"}{" "}
          · secure clinic access
          {user.role === "admin" ? " · read-only" : ""}
        </Badge>
        {(user.role === "staff" || user.role === "clinician") && (
          <Link
            className="inline-flex min-h-11 items-center justify-center gap-2 rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground"
            to="/patients/new"
          >
            <Plus className="size-4" /> Add patient
          </Link>
        )}
      </header>

      <section
        className="rounded-2xl border bg-card p-4"
        aria-label="Patient search"
      >
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="relative block w-full sm:max-w-xl">
            <Search className="pointer-events-none absolute left-3 top-3 size-4 text-muted-foreground" />
            <label className="sr-only" htmlFor="patient-directory-search">
              Search patients
            </label>
            <Input
              className="pl-9"
              id="patient-directory-search"
              onChange={(event) => {
                setSearch(event.target.value)
                setOffset(0)
              }}
              placeholder="Search by patient name, MRN, or date of birth"
              type="search"
              value={search}
            />
          </div>
          <p className="text-sm text-muted-foreground">
            {totalMatches} matching patient records
          </p>
        </div>
      </section>

      {(todayQuery.isLoading || previousQuery.isLoading) && (
        <p className="flex items-center gap-2 text-muted-foreground">
          <LoaderCircle className="animate-spin" /> Loading clinic patients…
        </p>
      )}
      {queryError && (
        <Alert className="border-critical/40 bg-critical-muted text-critical-muted-foreground">
          <AlertDescription>{apiErrorMessage(queryError)}</AlertDescription>
        </Alert>
      )}

      <section className="space-y-3" aria-labelledby="today-visits-heading">
        <div className="flex items-center gap-3">
          <CalendarClock className="size-5 text-primary" />
          <h2
            className="font-serif text-2xl font-semibold"
            id="today-visits-heading"
          >
            Today&apos;s visits
          </h2>
          <Badge variant="secondary">{todayQuery.data?.count ?? 0}</Badge>
        </div>
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {todayQuery.data?.data.map((patient) => (
            <PatientCard key={patient.id} patient={patient} today />
          ))}
        </div>
        {todayQuery.data?.data.length === 0 && (
          <div className="rounded-2xl border border-dashed bg-card px-5 py-8 text-center text-muted-foreground">
            {deferredSearch
              ? "No matching visits scheduled for today."
              : "No visits scheduled for today."}
          </div>
        )}
      </section>

      <section className="space-y-3" aria-labelledby="patient-records-heading">
        <div className="flex items-center gap-3">
          <History className="size-5 text-primary" />
          <h2
            className="font-serif text-2xl font-semibold"
            id="patient-records-heading"
          >
            Previous patient records
          </h2>
          <Badge variant="secondary">{previousQuery.data?.count ?? 0}</Badge>
        </div>
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {previousQuery.data?.data.map((patient) => (
            <PatientCard key={patient.id} patient={patient} today={false} />
          ))}
        </div>
        {previousQuery.data?.data.length === 0 && (
          <div className="rounded-2xl border border-dashed bg-card py-16 text-center">
            <UsersRound className="mx-auto mb-3 size-8 text-muted-foreground" />
            <p className="font-medium">No previous patient records found</p>
          </div>
        )}
      </section>

      {(previousQuery.data?.count ?? 0) > pageSize && (
        <nav
          className="flex items-center justify-between rounded-xl border bg-card p-3"
          aria-label="Previous patient record pages"
        >
          <Button
            disabled={offset === 0 || previousQuery.isFetching}
            onClick={() => setOffset(Math.max(0, offset - pageSize))}
            variant="outline"
          >
            Previous
          </Button>
          <span className="text-sm text-muted-foreground">
            {offset + 1}–
            {Math.min(offset + pageSize, previousQuery.data?.count ?? 0)} of{" "}
            {previousQuery.data?.count ?? 0}
          </span>
          <Button
            disabled={
              offset + pageSize >= (previousQuery.data?.count ?? 0) ||
              previousQuery.isFetching
            }
            onClick={() => setOffset(offset + pageSize)}
            variant="outline"
          >
            Next
          </Button>
        </nav>
      )}
    </div>
  )
}

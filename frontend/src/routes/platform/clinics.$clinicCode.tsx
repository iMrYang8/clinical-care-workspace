import { useQuery } from "@tanstack/react-query"
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router"
import { ArrowLeft, LoaderCircle } from "lucide-react"
import { useEffect, useState } from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { type PlatformPatient, platformApi } from "@/features/platformApi"
import { formatSingaporeDateTime } from "@/lib/dateTime"

export const Route = createFileRoute("/platform/clinics/$clinicCode")({
  component: PlatformClinicPage,
})

function PlatformClinicPage() {
  const { clinicCode } = Route.useParams()
  const navigate = useNavigate()
  const [selected, setSelected] = useState<PlatformPatient | null>(null)
  const me = useQuery({
    queryKey: ["platform", "me"],
    queryFn: platformApi.me,
    retry: false,
  })
  const patients = useQuery({
    queryKey: ["platform", clinicCode, "patients"],
    queryFn: () => platformApi.patients(clinicCode),
    enabled: me.isSuccess,
  })
  const timeline = useQuery({
    queryKey: ["platform", clinicCode, selected?.id, "timeline"],
    queryFn: () => platformApi.timeline(clinicCode, selected?.id ?? ""),
    enabled: Boolean(selected),
  })
  useEffect(() => {
    if (me.isError) void navigate({ to: "/platform/login", replace: true })
  }, [me.isError, navigate])
  if (me.isLoading)
    return <LoaderCircle className="mx-auto mt-24 animate-spin text-primary" />
  return (
    <main className="min-h-screen bg-background p-6 text-foreground">
      <div className="mx-auto max-w-6xl space-y-6">
        <Button asChild variant="ghost">
          <Link to="/platform">
            <ArrowLeft className="size-4" /> All clinics
          </Link>
        </Button>
        <header>
          <p className="text-xs font-bold uppercase tracking-[0.18em] text-primary">
            Read-only clinic view
          </p>
          <h1 className="font-serif text-4xl font-semibold">{clinicCode}</h1>
        </header>
        <div className="grid gap-6 lg:grid-cols-[20rem_1fr]">
          <section className="space-y-3">
            <h2 className="font-serif text-2xl font-semibold">Patients</h2>
            {patients.data?.map((patient) => (
              <button
                type="button"
                key={patient.id}
                className="w-full rounded-xl border bg-card p-4 text-left hover:border-primary"
                onClick={() => setSelected(patient)}
              >
                <p className="font-semibold">{patient.display_name}</p>
                <p className="text-sm text-muted-foreground">
                  MRN {patient.medical_record_number ?? "Not recorded"} · ID{" "}
                  {patient.masked_identity_document ?? "Not recorded"}
                </p>
              </button>
            ))}
          </section>
          <section>
            {!selected ? (
              <Card>
                <CardContent className="p-10 text-center text-muted-foreground">
                  Select a patient to inspect the care timeline.
                </CardContent>
              </Card>
            ) : (
              <div className="space-y-4">
                <Card>
                  <CardHeader>
                    <CardTitle>{selected.display_name}</CardTitle>
                  </CardHeader>
                  <CardContent className="flex flex-wrap gap-2">
                    <Badge variant="outline">
                      DOB {selected.date_of_birth ?? "Not recorded"}
                    </Badge>
                    <Badge variant="outline">
                      Portal {selected.portal_access_state}
                    </Badge>
                    <Badge>Read only</Badge>
                  </CardContent>
                </Card>
                {timeline.isLoading && (
                  <LoaderCircle className="animate-spin text-primary" />
                )}
                {timeline.data?.map((entry) => (
                  <Card key={entry.id}>
                    <CardHeader>
                      <CardTitle className="text-lg">{entry.title}</CardTitle>
                      <p className="text-xs text-muted-foreground">
                        {formatSingaporeDateTime(entry.occurred_at)} ·{" "}
                        {entry.section}
                      </p>
                    </CardHeader>
                    <CardContent>
                      <p className="whitespace-pre-wrap leading-7">
                        {entry.content}
                      </p>
                    </CardContent>
                  </Card>
                ))}
              </div>
            )}
          </section>
        </div>
      </div>
    </main>
  )
}

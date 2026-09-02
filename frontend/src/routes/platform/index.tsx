import { useQuery, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router"
import { Building2, LoaderCircle, LogOut, ShieldCheck } from "lucide-react"
import { useEffect } from "react"
import { OnboardClinicDialog } from "@/components/Platform/OnboardClinicDialog"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { platformApi } from "@/features/platformApi"

export const Route = createFileRoute("/platform/")({
  component: PlatformDashboard,
})

function PlatformDashboard() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const me = useQuery({
    queryKey: ["platform", "me"],
    queryFn: platformApi.me,
    retry: false,
  })
  const clinics = useQuery({
    queryKey: ["platform", "clinics"],
    queryFn: platformApi.clinics,
    enabled: me.isSuccess,
  })
  useEffect(() => {
    if (me.isError) void navigate({ to: "/platform/login", replace: true })
  }, [me.isError, navigate])
  if (me.isLoading)
    return <LoaderCircle className="mx-auto mt-24 animate-spin text-primary" />
  if (!me.data) return null
  return (
    <main className="min-h-screen bg-background text-foreground">
      <header className="border-b bg-card">
        <div className="mx-auto flex max-w-6xl items-center justify-between p-5">
          <div>
            <p className="text-xs font-bold uppercase tracking-[0.18em] text-primary">
              Nightingale
            </p>
            <h1 className="font-serif text-2xl font-semibold">
              Platform oversight
            </h1>
          </div>
          <Button
            variant="outline"
            onClick={async () => {
              await platformApi.logout()
              await navigate({ to: "/platform/login" })
            }}
          >
            <LogOut className="size-4" /> Sign out
          </Button>
        </div>
      </header>
      <div className="mx-auto max-w-6xl space-y-6 p-6">
        <Alert>
          <ShieldCheck className="size-4" />
          <AlertDescription>
            Signed in as {me.data.full_name ?? me.data.email}. Cross-clinic
            views are read-only and recorded in the platform audit log.
          </AlertDescription>
        </Alert>
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="font-serif text-3xl font-semibold">Clinics</h2>
            <p className="text-muted-foreground">
              Select a clinic to inspect its patient records.
            </p>
          </div>
          <OnboardClinicDialog
            onCreated={() =>
              queryClient.invalidateQueries({
                queryKey: ["platform", "clinics"],
              })
            }
          />
        </div>
        {clinics.isLoading && (
          <LoaderCircle className="animate-spin text-primary" />
        )}
        <div className="grid gap-4 md:grid-cols-2">
          {clinics.data?.map((clinic) => (
            <Link
              key={clinic.id}
              to="/platform/clinics/$clinicCode"
              params={{ clinicCode: clinic.code }}
            >
              <Card className="h-full transition hover:border-primary/50">
                <CardHeader>
                  <CardTitle className="flex items-center gap-2">
                    <Building2 className="text-primary" /> {clinic.name}
                  </CardTitle>
                </CardHeader>
                <CardContent className="flex gap-2">
                  <Badge variant="outline">{clinic.member_count} members</Badge>
                  <Badge variant="outline">
                    {clinic.patient_count} patients
                  </Badge>
                </CardContent>
              </Card>
            </Link>
          ))}
        </div>
      </div>
    </main>
  )
}

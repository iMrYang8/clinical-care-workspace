import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { FileLock2, LoaderCircle, ShieldCheck, Stethoscope } from "lucide-react"
import { useEffect } from "react"

import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import useAuth, { roleHome } from "@/hooks/useAuth"

export const Route = createFileRoute("/_layout/admin")({
  component: AdminBoundary,
  head: () => ({ meta: [{ title: "Administration · Nightingale" }] }),
})

function AdminBoundary() {
  const navigate = useNavigate()
  const { user, meQuery } = useAuth()
  const allowed = user?.role === "admin" || user?.role === "worker"

  useEffect(() => {
    if (user && !allowed)
      void navigate({ to: roleHome(user.role), replace: true })
  }, [allowed, navigate, user])

  if (meQuery.isLoading || !user || !allowed) {
    return (
      <LoaderCircle className="mx-auto mt-24 animate-spin text-slate-600" />
    )
  }

  return (
    <div className="space-y-6">
      <header className="rounded-2xl border bg-white p-6">
        <Badge className="bg-slate-100 text-slate-700">
          {user.role} boundary
        </Badge>
        <h1 className="mt-3 font-serif text-4xl font-semibold text-slate-950">
          Clinic administration
        </h1>
        <p className="mt-2 max-w-3xl leading-7 text-slate-600">
          Administration is intentionally separate from clinical authorship.
          This persona manages membership and audit scope; it does not open or
          modify care note text.
        </p>
      </header>
      <div className="grid gap-4 md:grid-cols-3">
        {[
          [
            ShieldCheck,
            "Membership roles",
            "Patient, staff, clinician, admin, and worker are server-owned clinic memberships.",
          ],
          [
            FileLock2,
            "Audit visibility",
            "Clinical changes are immutable and attributed; the audit API is not exposed in this milestone.",
          ],
          [
            Stethoscope,
            "Clinical boundary",
            "Admin cannot edit clinical body text. This page makes that restriction visible.",
          ],
        ].map(([Icon, title, description]) => (
          <Card className="border-slate-200 bg-white" key={String(title)}>
            <CardHeader>
              <Icon className="text-slate-700" />
              <CardTitle className="font-serif text-xl">
                {title as string}
              </CardTitle>
            </CardHeader>
            <CardContent className="text-sm leading-6 text-slate-600">
              {description as string}
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  )
}

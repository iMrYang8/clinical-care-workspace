import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { FileLock2, LoaderCircle, ShieldCheck, UserPlus } from "lucide-react"
import { useEffect, useState } from "react"

import type { MembershipCreate } from "@/client"
import { SessionBoundaryError } from "@/components/Nightingale/SessionBoundaryError"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { adminApi, apiErrorMessage } from "@/features/api"
import useAuth, { roleHome } from "@/hooks/useAuth"

export const Route = createFileRoute("/_layout/admin")({
  component: AdminBoundary,
  head: () => ({ meta: [{ title: "Administration · Nightingale" }] }),
})

const initialInvite: MembershipCreate = {
  email: "",
  full_name: "",
  role: "staff",
}

function AdminBoundary() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { user, meQuery, logout } = useAuth()
  const [invite, setInvite] = useState<MembershipCreate>(initialInvite)
  const [inviteStatus, setInviteStatus] = useState<string>()
  const allowed = user?.role === "admin"

  useEffect(() => {
    if (user && !allowed)
      void navigate({ to: roleHome(user.role), replace: true })
  }, [allowed, navigate, user])

  const memberships = useQuery({
    queryKey: ["admin", "memberships"],
    queryFn: adminApi.memberships,
    enabled: allowed,
  })
  const audit = useQuery({
    queryKey: ["admin", "audit"],
    queryFn: adminApi.auditEvents,
    enabled: allowed,
  })
  const refreshMemberships = () =>
    queryClient.invalidateQueries({ queryKey: ["admin", "memberships"] })
  const createMembership = useMutation({
    mutationFn: adminApi.createMembership,
    onSuccess: async (created) => {
      setInviteStatus(
        `Invitation sent to ${created.email}; no membership exists until the recipient verifies the one-time code.`,
      )
      setInvite(initialInvite)
      await Promise.all([
        refreshMemberships(),
        queryClient.invalidateQueries({ queryKey: ["admin", "audit"] }),
      ])
    },
  })
  const deactivate = useMutation({
    mutationFn: adminApi.deactivateMembership,
    onSuccess: refreshMemberships,
  })

  if (meQuery.isError) {
    return <SessionBoundaryError error={meQuery.error} onClear={logout} />
  }
  if (meQuery.isLoading || !user || !allowed) {
    return (
      <LoaderCircle className="mx-auto mt-24 animate-spin text-slate-600" />
    )
  }

  const requestError =
    memberships.error ??
    audit.error ??
    createMembership.error ??
    deactivate.error

  return (
    <div className="space-y-6">
      <header className="rounded-2xl border bg-white p-6">
        <Badge className="bg-slate-100 text-slate-700">Admin boundary</Badge>
        <h1 className="mt-3 font-serif text-4xl font-semibold text-slate-950">
          Clinic administration
        </h1>
        <p className="mt-2 max-w-3xl leading-7 text-slate-600">
          Manage this clinic&apos;s memberships and inspect metadata-only audit
          events. Clinical titles, bodies, comments, and AI payloads never enter
          this route or its API responses.
        </p>
      </header>

      {requestError && (
        <Alert className="border-red-200 bg-red-50 text-red-900">
          <AlertDescription>{apiErrorMessage(requestError)}</AlertDescription>
        </Alert>
      )}

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1.4fr)_minmax(22rem,0.6fr)]">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 font-serif text-2xl">
              <ShieldCheck /> Clinic memberships
            </CardTitle>
          </CardHeader>
          <CardContent className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="border-b text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="px-2 py-3">Member</th>
                  <th className="px-2 py-3">Role</th>
                  <th className="px-2 py-3">State</th>
                  <th className="px-2 py-3 text-right">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {memberships.data?.map((membership) => (
                  <tr key={membership.id}>
                    <td className="px-2 py-3">
                      <span className="block font-medium">
                        {membership.full_name ?? "Unnamed member"}
                      </span>
                      <span className="text-xs text-slate-500">
                        {membership.email}
                      </span>
                    </td>
                    <td className="px-2 py-3">{membership.role}</td>
                    <td className="px-2 py-3">
                      <Badge
                        variant={membership.is_active ? "default" : "secondary"}
                      >
                        {membership.is_active ? "Active" : "Inactive"}
                      </Badge>
                    </td>
                    <td className="px-2 py-3 text-right">
                      <Button
                        disabled={
                          !membership.is_active ||
                          membership.id === user.membership_id ||
                          deactivate.isPending
                        }
                        onClick={() => deactivate.mutate(membership.id)}
                        size="sm"
                        variant="outline"
                      >
                        Deactivate
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 font-serif text-2xl">
              <UserPlus /> Invite synthetic member
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="invite-email">Email</Label>
              <Input
                id="invite-email"
                onChange={(event) =>
                  setInvite({ ...invite, email: event.target.value })
                }
                type="email"
                value={invite.email}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="invite-name">Display name</Label>
              <Input
                id="invite-name"
                onChange={(event) =>
                  setInvite({ ...invite, full_name: event.target.value })
                }
                value={invite.full_name ?? ""}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="invite-role">Role</Label>
              <select
                className="h-10 w-full rounded-md border bg-white px-3 text-sm"
                id="invite-role"
                onChange={(event) =>
                  setInvite({
                    ...invite,
                    role: event.target.value as MembershipCreate["role"],
                  })
                }
                value={invite.role}
              >
                <option value="patient">Patient</option>
                <option value="staff">Staff</option>
                <option value="clinician">Clinician</option>
                <option value="admin">Admin</option>
              </select>
            </div>
            <div className="space-y-2">
              <p className="rounded-md border border-teal-100 bg-teal-50 p-3 text-sm text-teal-950">
                Nightingale emails a 24-hour one-time code. The recipient—not
                the admin—verifies the address and chooses the account password
                before the membership becomes active.
              </p>
            </div>
            {inviteStatus && (
              <Alert className="border-emerald-200 bg-emerald-50 text-emerald-950">
                <AlertDescription>{inviteStatus}</AlertDescription>
              </Alert>
            )}
            <Button
              className="w-full"
              disabled={createMembership.isPending || !invite.email}
              onClick={() => createMembership.mutate(invite)}
            >
              {createMembership.isPending && (
                <LoaderCircle className="animate-spin" />
              )}
              Send verified invitation
            </Button>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 font-serif text-2xl">
            <FileLock2 /> Metadata-only audit trail
          </CardTitle>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="border-b text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-2 py-3">Time</th>
                <th className="px-2 py-3">Actor</th>
                <th className="px-2 py-3">Action</th>
                <th className="px-2 py-3">Resource</th>
                <th className="px-2 py-3">Version</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {audit.data?.map((event) => (
                <tr key={event.id}>
                  <td className="whitespace-nowrap px-2 py-3">
                    {new Date(event.created_at).toLocaleString()}
                  </td>
                  <td className="px-2 py-3 font-mono text-xs">
                    {event.actor_id.slice(0, 8)}
                  </td>
                  <td className="px-2 py-3">{event.action}</td>
                  <td className="px-2 py-3 font-mono text-xs">
                    {event.resource_type}:{event.resource_id.slice(0, 8)}
                  </td>
                  <td className="px-2 py-3 font-mono text-xs">
                    {event.version_id?.slice(0, 8) ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardContent>
      </Card>
    </div>
  )
}

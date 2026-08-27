import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import {
  Bot,
  FileLock2,
  KeyRound,
  LoaderCircle,
  ShieldCheck,
  UserPlus,
} from "lucide-react"
import { type FormEvent, useEffect, useState } from "react"

import type { MembershipCreate } from "@/client"
import { SessionBoundaryError } from "@/components/Nightingale/SessionBoundaryError"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  adminApi,
  apiErrorMessage,
  type ClinicAISettingUpdate,
} from "@/features/api"
import useAuth, { roleHome } from "@/hooks/useAuth"
import { formatSingaporeDateTime } from "@/lib/dateTime"

export const Route = createFileRoute("/_layout/admin")({
  component: AdminBoundary,
  head: () => ({ meta: [{ title: "Administration · Nightingale" }] }),
})

const initialInvite: MembershipCreate = {
  email: "",
  full_name: "",
  role: "staff",
}

const initialAISettings: ClinicAISettingUpdate = {
  api_key: "",
  clear_api_key: false,
  fast_model: "gpt-5-mini",
  careful_model: "gpt-5.1",
  transcribe_model: "gpt-4o-transcribe-diarize",
}

const activityLabels: Record<string, string> = {
  "membership.invited": "Team invitation sent",
  "membership.invitation_delivery_failed": "Invitation delivery failed",
  "membership.invitation_accepted": "Invitation accepted",
  "membership.deactivated": "Team access deactivated",
  "patient.created": "Patient record created",
  "patient.portal_invited": "Patient portal invitation sent",
  "patient.portal_invitation_accepted": "Patient portal activated",
  "entry.created": "Care note created",
  "entry.updated": "Care note updated",
  "entry.reverted": "Earlier note restored",
  "entry.patient_sharing_requested": "Patient sharing requested",
  "entry.patient_sharing_approved": "Patient sharing approved",
  "comment.created": "Team discussion started",
  "comment.resolved": "Team discussion resolved",
  "comment.assigned": "Discussion assigned",
  "highlight.created": "Priority identified",
  "highlight.accept": "Priority accepted",
  "highlight.reject": "Priority rejected",
  "highlight.pin": "Priority pinned",
  "highlight.review_requested": "Clinical review requested",
  "conflict.resolved": "Clinical conflict resolved",
  "voice.session_created": "Visit recording started",
  "voice.device_joined": "Recording device joined",
  "voice.device_sealed": "Recording device completed",
  "voice.finalized": "Visit recording finalized",
  "voice.transcript_corrected": "Transcript corrected",
  "voice.published": "Reviewed visit note published",
  "voice.processing_completed": "Visit recording processed",
  "job.created": "Processing job queued",
  "job.exhausted": "Processing job needs review",
  "ai.completed": "AI-assisted processing completed",
  "ai.failed": "AI-assisted processing needs review",
  "entry_version.archived": "Historical record archived",
  "entry_version.rehydrated": "Historical record restored",
  "patient_publication.withdrawn": "Patient sharing withdrawn",
  "clinic.ai_settings.updated": "AI processing settings updated",
}

function activityLabel(action: string): string {
  if (activityLabels[action]) return activityLabels[action]
  if (action.startsWith("highlight.feedback.dismiss"))
    return "Priority feedback recorded"
  return action
    .replace(/[._]/g, " ")
    .replace(/\b\w/g, (letter: string) => letter.toUpperCase())
}

const roleLabels: Record<string, string> = {
  staff: "Care staff",
  clinician: "Clinician",
  admin: "Clinic administrator",
}

const activityAreaLabels: Record<string, string> = {
  membership: "Team access",
  entry: "Care note",
  entry_version: "Care note history",
  comment: "Team discussion",
  highlight: "Current priorities",
  voice_session: "Visit recording",
  voice_device: "Visit recording",
  patient: "Patient record",
  patient_portal_invitation: "Patient portal",
  patient_sharing_request: "Patient sharing",
  conflict: "Clinical review",
  job: "Care processing",
  ai_run: "AI-assisted processing",
  clinic_ai_setting: "AI processing",
  archive_blob: "Historical retention",
  decay_run: "Historical retention",
}

function AdminBoundary() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { user, meQuery, logout } = useAuth()
  const [invite, setInvite] = useState<MembershipCreate>(initialInvite)
  const [inviteStatus, setInviteStatus] = useState<string>()
  const [inviteOpen, setInviteOpen] = useState(false)
  const [aiForm, setAIForm] = useState<ClinicAISettingUpdate>(initialAISettings)
  const [aiStatus, setAIStatus] = useState<string>()
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
  const aiSettings = useQuery({
    queryKey: ["admin", "ai-settings"],
    queryFn: adminApi.clinicAISettings,
    enabled: allowed,
  })
  useEffect(() => {
    if (!aiSettings.data) return
    setAIForm({
      api_key: "",
      clear_api_key: false,
      fast_model: aiSettings.data.fast_model,
      careful_model: aiSettings.data.careful_model,
      transcribe_model: aiSettings.data.transcribe_model,
    })
  }, [aiSettings.data])
  const refreshMemberships = () =>
    queryClient.invalidateQueries({ queryKey: ["admin", "memberships"] })
  const createMembership = useMutation({
    mutationFn: adminApi.createMembership,
    onSuccess: async (created) => {
      setInviteStatus(
        `Invitation sent to ${created.email}; no membership exists until the recipient verifies the one-time code.`,
      )
      setInvite(initialInvite)
      setInviteOpen(false)
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
  const saveAISettings = useMutation({
    mutationFn: adminApi.updateClinicAISettings,
    onSuccess: async (saved) => {
      setAIStatus(
        `AI processing settings saved${saved.api_key_configured ? ` · clinic key ending ${saved.api_key_last4}` : saved.credential_source === "environment" ? " · server environment credential active" : " · no credential configured"}.`,
      )
      setAIForm((current) => ({
        ...current,
        api_key: "",
        clear_api_key: false,
      }))
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["admin", "ai-settings"] }),
        queryClient.invalidateQueries({ queryKey: ["admin", "audit"] }),
      ])
    },
  })

  if (meQuery.isError) {
    return <SessionBoundaryError error={meQuery.error} onClear={logout} />
  }
  if (meQuery.isLoading || !user || !allowed) {
    return (
      <LoaderCircle className="mx-auto mt-24 animate-spin text-muted-foreground" />
    )
  }

  const requestError =
    memberships.error ??
    audit.error ??
    aiSettings.error ??
    createMembership.error ??
    deactivate.error ??
    saveAISettings.error
  const submitInvitation = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    createMembership.mutate({
      ...invite,
      email: invite.email.trim().toLowerCase(),
      full_name: invite.full_name?.trim() || null,
    })
  }
  const memberName = (userId: string) =>
    memberships.data?.find((membership) => membership.user_id === userId)
      ?.full_name ?? "Clinic team member"
  const submitAISettings = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    saveAISettings.mutate({
      ...aiForm,
      api_key: aiForm.api_key?.trim() || null,
    })
  }

  return (
    <div className="space-y-6">
      <header className="rounded-2xl border bg-card p-6">
        <Badge className="bg-muted text-muted-foreground">Administration</Badge>
        <h1 className="mt-3 font-serif text-4xl font-semibold text-foreground">
          Clinic administration
        </h1>
        <p className="mt-2 max-w-3xl leading-7 text-muted-foreground">
          Manage team access and review clinic activity. Clinical documentation
          remains read-only for administrators.
        </p>
      </header>

      {requestError && (
        <Alert className="border-critical/40 bg-critical-muted text-critical-muted-foreground">
          <AlertDescription>{apiErrorMessage(requestError)}</AlertDescription>
        </Alert>
      )}

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1.4fr)_minmax(22rem,0.6fr)]">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 font-serif text-2xl">
              <ShieldCheck /> Team access
            </CardTitle>
          </CardHeader>
          <CardContent className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="border-b text-xs uppercase tracking-wide text-muted-foreground">
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
                      <span className="text-xs text-muted-foreground">
                        {membership.email}
                      </span>
                    </td>
                    <td className="px-2 py-3">
                      {roleLabels[membership.role] ?? "Clinic team member"}
                    </td>
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
              <UserPlus /> Invite team member
            </CardTitle>
            <p className="text-sm text-muted-foreground">
              Add authorized care staff, clinicians, or clinic administrators.
            </p>
          </CardHeader>
          <CardContent>
            <Button className="w-full" onClick={() => setInviteOpen(true)}>
              <UserPlus /> New invitation
            </Button>
            {inviteStatus && (
              <Alert className="mt-4 border-success/40 bg-success-muted text-success-muted-foreground">
                <AlertDescription>{inviteStatus}</AlertDescription>
              </Alert>
            )}
          </CardContent>
        </Card>
        <Dialog
          open={inviteOpen}
          onOpenChange={(open) =>
            !createMembership.isPending && setInviteOpen(open)
          }
        >
          <DialogContent className="sm:max-w-lg">
            <DialogHeader>
              <DialogTitle className="font-serif text-2xl">
                Invite team member
              </DialogTitle>
              <DialogDescription>
                The recipient verifies a 24-hour one-time invitation and sets
                their own password.
              </DialogDescription>
            </DialogHeader>
            <form className="space-y-4" onSubmit={submitInvitation}>
              <div className="space-y-2">
                <Label htmlFor="invite-email">Email</Label>
                <Input
                  autoFocus
                  id="invite-email"
                  onChange={(event) =>
                    setInvite({ ...invite, email: event.target.value })
                  }
                  type="email"
                  required
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
                  className="h-10 w-full rounded-md border bg-background px-3 text-sm text-foreground"
                  id="invite-role"
                  onChange={(event) =>
                    setInvite({
                      ...invite,
                      role: event.target.value as MembershipCreate["role"],
                    })
                  }
                  value={invite.role}
                >
                  <option value="staff">Care staff</option>
                  <option value="clinician">Clinician</option>
                  <option value="admin">Clinic administrator</option>
                </select>
              </div>
              <div className="space-y-2">
                <p className="rounded-md border border-primary/30 bg-primary/10 p-3 text-sm text-foreground">
                  Nightingale emails a 24-hour one-time code. The recipient—not
                  the admin—verifies the address and chooses the account
                  password before the membership becomes active.
                </p>
                <p className="text-xs leading-5 text-muted-foreground">
                  Patient onboarding is a separate patient-record linking flow;
                  this care-team invitation form cannot create patient access.
                </p>
              </div>
              <DialogFooter>
                <Button
                  disabled={createMembership.isPending}
                  onClick={() => setInviteOpen(false)}
                  type="button"
                  variant="outline"
                >
                  Cancel
                </Button>
                <Button
                  disabled={createMembership.isPending || !invite.email}
                  type="submit"
                >
                  {createMembership.isPending && (
                    <LoaderCircle className="animate-spin" />
                  )}
                  Send verified invitation
                </Button>
              </DialogFooter>
            </form>
          </DialogContent>
        </Dialog>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 font-serif text-2xl">
            <Bot /> AI processing
          </CardTitle>
          <p className="max-w-3xl text-sm leading-6 text-muted-foreground">
            Configure one encrypted OpenAI credential for this clinic. Routine
            extraction uses the fast route; high-risk review, conflicts, and
            patient-sharing checks use the careful route. The credential is
            never shown again or returned to clinic users.
          </p>
        </CardHeader>
        <CardContent>
          <form
            className="grid gap-5 lg:grid-cols-2"
            onSubmit={submitAISettings}
          >
            <div className="space-y-2 lg:col-span-2">
              <Label htmlFor="clinic-openai-key">OpenAI API key</Label>
              <div className="flex flex-col gap-2 sm:flex-row">
                <div className="relative flex-1">
                  <KeyRound className="absolute left-3 top-3 size-4 text-muted-foreground" />
                  <Input
                    autoComplete="off"
                    className="pl-9"
                    id="clinic-openai-key"
                    onChange={(event) =>
                      setAIForm({
                        ...aiForm,
                        api_key: event.target.value,
                        clear_api_key: false,
                      })
                    }
                    placeholder={
                      aiSettings.data?.api_key_configured
                        ? `Configured · ending ${aiSettings.data.api_key_last4}`
                        : aiSettings.data?.credential_source === "environment"
                          ? "Server environment credential is active · paste to replace for this clinic"
                          : "Paste a clinic-owned API key"
                    }
                    type="password"
                    value={aiForm.api_key ?? ""}
                  />
                </div>
                {aiSettings.data?.api_key_configured && (
                  <Button
                    onClick={() =>
                      setAIForm({ ...aiForm, api_key: "", clear_api_key: true })
                    }
                    type="button"
                    variant={aiForm.clear_api_key ? "destructive" : "outline"}
                  >
                    {aiForm.clear_api_key
                      ? "Key will be removed"
                      : "Remove key"}
                  </Button>
                )}
              </div>
              <p className="text-xs text-muted-foreground">
                Stored with clinic-scoped AES-256-GCM encryption. Saving a new
                value replaces the previous key; existing keys are never
                revealed.
                {aiSettings.data?.credential_source === "environment" &&
                  " This clinic currently inherits the server environment credential."}
              </p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="fast-model">
                Fast model · routine processing
              </Label>
              <Input
                id="fast-model"
                onChange={(event) =>
                  setAIForm({ ...aiForm, fast_model: event.target.value })
                }
                required
                value={aiForm.fast_model}
              />
              <p className="text-xs text-muted-foreground">
                Transcription follow-up, structured extraction, and routine
                summaries.
              </p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="careful-model">
                Careful model · high-risk review
              </Label>
              <Input
                id="careful-model"
                onChange={(event) =>
                  setAIForm({ ...aiForm, careful_model: event.target.value })
                }
                required
                value={aiForm.careful_model}
              />
              <p className="text-xs text-muted-foreground">
                Used only when deterministic rules detect elevated risk or
                conflicts.
              </p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="transcribe-model">
                Visit transcription model
              </Label>
              <Input
                id="transcribe-model"
                onChange={(event) =>
                  setAIForm({ ...aiForm, transcribe_model: event.target.value })
                }
                required
                value={aiForm.transcribe_model}
              />
            </div>
            <div className="flex items-end">
              <Button
                className="w-full"
                disabled={saveAISettings.isPending}
                type="submit"
              >
                {saveAISettings.isPending && (
                  <LoaderCircle className="animate-spin" />
                )}
                Save AI processing settings
              </Button>
            </div>
          </form>
          {aiStatus && (
            <Alert className="mt-4 border-success/40 bg-success-muted text-success-muted-foreground">
              <AlertDescription>{aiStatus}</AlertDescription>
            </Alert>
          )}
          <p className="mt-4 rounded-lg border bg-muted/35 p-3 text-xs leading-5 text-muted-foreground">
            Clinic-level credentials do not bypass privacy gates. Remote text
            and audio processing still stops when redaction qualification,
            egress controls, calibration, or clinical-review requirements fail.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 font-serif text-2xl">
            <FileLock2 /> Activity log
          </CardTitle>
          <p className="text-sm text-muted-foreground">
            Database-backed audit events · Singapore Time (SGT)
          </p>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="border-b text-xs uppercase tracking-wide text-muted-foreground">
              <tr>
                <th className="px-2 py-3">Time</th>
                <th className="px-2 py-3">Actor</th>
                <th className="px-2 py-3">Action</th>
                <th className="px-2 py-3">Area</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {audit.data?.map((event) => (
                <tr key={event.id}>
                  <td className="whitespace-nowrap px-2 py-3">
                    {formatSingaporeDateTime(event.created_at)}
                  </td>
                  <td className="px-2 py-3">{memberName(event.actor_id)}</td>
                  <td className="px-2 py-3">{activityLabel(event.action)}</td>
                  <td className="px-2 py-3">
                    {activityAreaLabels[event.resource_type] ??
                      "Clinic activity"}
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

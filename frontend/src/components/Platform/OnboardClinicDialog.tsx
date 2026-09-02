import { useMutation } from "@tanstack/react-query"
import {
  Building2,
  CheckCircle2,
  LoaderCircle,
  TriangleAlert,
} from "lucide-react"
import { useState } from "react"

import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
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
  type ClinicOnboardingInput,
  type ClinicPreflight,
  platformApi,
} from "@/features/platformApi"

const initialClinic: ClinicOnboardingInput = {
  code: "",
  slug: "",
  name: "",
  timezone: "Asia/Singapore",
  initial_staff: [{ email: "", full_name: "", role: "admin" }],
  worker_enabled: true,
  supported_languages: ["en", "ms", "nan"],
  messaging_channels: ["email", "sms", "whatsapp"],
  remote_text_egress_enabled: false,
  remote_audio_egress_enabled: false,
  calibration_required: true,
}

function toggle<Value extends string>(values: Value[], value: Value): Value[] {
  return values.includes(value)
    ? values.filter((item) => item !== value)
    : [...values, value]
}

const supportedLanguageOptions = [
  ["en", "English"],
  ["ms", "Malay"],
  ["nan", "Hokkien / Southern Min"],
  ["zh", "Chinese"],
] as const

const messagingChannelOptions = [
  ["email", "Email"],
  ["sms", "SMS"],
  ["whatsapp", "WhatsApp"],
] as const

export function OnboardClinicDialog({
  onCreated,
}: {
  onCreated: () => void | Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState(initialClinic)
  const [preflight, setPreflight] = useState<ClinicPreflight | null>(null)
  const preflightMutation = useMutation({
    mutationFn: () =>
      platformApi.preflightClinic({
        ...form,
        code: form.code.trim().toUpperCase(),
        slug: form.slug.trim().toLowerCase(),
        initial_staff: form.initial_staff.map((member) => ({
          ...member,
          email: member.email.trim().toLowerCase(),
          full_name: member.full_name.trim(),
        })),
      }),
    onSuccess: setPreflight,
  })
  const onboardMutation = useMutation({
    mutationFn: () =>
      platformApi.onboardClinic({
        ...form,
        code: form.code.trim().toUpperCase(),
        slug: form.slug.trim().toLowerCase(),
        initial_staff: form.initial_staff.map((member) => ({
          ...member,
          email: member.email.trim().toLowerCase(),
          full_name: member.full_name.trim(),
        })),
      }),
    onSuccess: async () => {
      setOpen(false)
      setForm(initialClinic)
      setPreflight(null)
      await onCreated()
    },
  })

  return (
    <>
      <Button onClick={() => setOpen(true)}>
        <Building2 /> Onboard clinic
      </Button>
      <Dialog
        onOpenChange={(value) => {
          if (!onboardMutation.isPending) setOpen(value)
        }}
        open={open}
      >
        <DialogContent className="max-h-[92vh] overflow-y-auto sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle className="font-serif text-2xl">
              Onboard a clinic
            </DialogTitle>
            <DialogDescription>
              A second clinic is configuration and data only. Preflight checks
              identity, worker, language, messaging, egress, and calibration
              readiness before any clinic is created.
            </DialogDescription>
          </DialogHeader>
          <form
            className="space-y-5"
            onSubmit={(event) => {
              event.preventDefault()
              if (preflight?.ready) onboardMutation.mutate()
              else preflightMutation.mutate()
            }}
          >
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-2">
                <Label htmlFor="onboard-code">Clinic code</Label>
                <Input
                  id="onboard-code"
                  onChange={(event) => {
                    setForm({ ...form, code: event.target.value.toUpperCase() })
                    setPreflight(null)
                  }}
                  pattern="[A-Za-z]{3,12}"
                  required
                  value={form.code}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="onboard-name">Clinic name</Label>
                <Input
                  id="onboard-name"
                  onChange={(event) => {
                    setForm({ ...form, name: event.target.value })
                    setPreflight(null)
                  }}
                  required
                  value={form.name}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="onboard-slug">Clinic URL slug</Label>
                <Input
                  id="onboard-slug"
                  onChange={(event) => {
                    setForm({
                      ...form,
                      slug: event.target.value
                        .toLowerCase()
                        .replace(/[^a-z0-9-]/g, ""),
                    })
                    setPreflight(null)
                  }}
                  pattern="[a-z0-9-]{3,80}"
                  required
                  value={form.slug}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="onboard-timezone">Timezone</Label>
                <Input
                  id="onboard-timezone"
                  onChange={(event) => {
                    setForm({ ...form, timezone: event.target.value })
                    setPreflight(null)
                  }}
                  required
                  value={form.timezone}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="onboard-admin-email">Initial staff email</Label>
                <Input
                  id="onboard-admin-email"
                  onChange={(event) => {
                    setForm({
                      ...form,
                      initial_staff: [
                        {
                          ...form.initial_staff[0],
                          email: event.target.value,
                        },
                      ],
                    })
                    setPreflight(null)
                  }}
                  required
                  type="email"
                  value={form.initial_staff[0]?.email ?? ""}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="onboard-admin-name">Initial staff name</Label>
                <Input
                  id="onboard-admin-name"
                  onChange={(event) => {
                    setForm({
                      ...form,
                      initial_staff: [
                        {
                          ...form.initial_staff[0],
                          full_name: event.target.value,
                        },
                      ],
                    })
                    setPreflight(null)
                  }}
                  required
                  value={form.initial_staff[0]?.full_name ?? ""}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="onboard-admin-role">Initial role</Label>
                <select
                  className="h-10 w-full rounded-md border bg-background px-3 text-sm"
                  id="onboard-admin-role"
                  onChange={(event) => {
                    setForm({
                      ...form,
                      initial_staff: [
                        {
                          ...form.initial_staff[0],
                          role: event.target.value as
                            | "admin"
                            | "clinician"
                            | "staff",
                        },
                      ],
                    })
                    setPreflight(null)
                  }}
                  value={form.initial_staff[0]?.role ?? "admin"}
                >
                  <option value="admin">Clinic administrator</option>
                  <option value="clinician">Clinician</option>
                  <option value="staff">Care staff</option>
                </select>
              </div>
            </div>

            <fieldset className="space-y-2 rounded-xl border p-3">
              <legend className="px-1 text-sm font-semibold">
                Supported languages
              </legend>
              <div className="flex flex-wrap gap-3">
                {supportedLanguageOptions.map(([value, label]) => (
                  <label
                    className="flex items-center gap-2 text-sm"
                    key={value}
                  >
                    <input
                      checked={form.supported_languages.includes(value)}
                      onChange={() => {
                        setForm({
                          ...form,
                          supported_languages: toggle(
                            form.supported_languages,
                            value,
                          ),
                        })
                        setPreflight(null)
                      }}
                      type="checkbox"
                    />
                    {label}
                  </label>
                ))}
              </div>
            </fieldset>

            <fieldset className="space-y-2 rounded-xl border p-3">
              <legend className="px-1 text-sm font-semibold">
                Messaging channels
              </legend>
              <div className="flex flex-wrap gap-3">
                {messagingChannelOptions.map(([value, label]) => (
                  <label
                    className="flex items-center gap-2 text-sm"
                    key={value}
                  >
                    <input
                      checked={form.messaging_channels.includes(value)}
                      onChange={() => {
                        setForm({
                          ...form,
                          messaging_channels: toggle(
                            form.messaging_channels,
                            value,
                          ),
                        })
                        setPreflight(null)
                      }}
                      type="checkbox"
                    />
                    {label}
                  </label>
                ))}
              </div>
            </fieldset>

            <fieldset className="grid gap-2 rounded-xl border p-3 sm:grid-cols-2">
              <legend className="px-1 text-sm font-semibold">
                Operational policy
              </legend>
              {[
                ["worker_enabled", "Background worker enabled"],
                ["calibration_required", "Calibration required"],
                ["remote_text_egress_enabled", "Remote redacted text enabled"],
                ["remote_audio_egress_enabled", "Remote PHI audio enabled"],
              ].map(([key, label]) => (
                <label className="flex items-center gap-2 text-sm" key={key}>
                  <input
                    checked={Boolean(form[key as keyof ClinicOnboardingInput])}
                    onChange={(event) => {
                      setForm({ ...form, [key]: event.target.checked })
                      setPreflight(null)
                    }}
                    type="checkbox"
                  />
                  {label}
                </label>
              ))}
              <p className="col-span-full text-xs leading-5 text-muted-foreground">
                Remote audio is off by default and requires explicit clinic
                policy plus patient consent. Local ASR remains the default.
              </p>
            </fieldset>

            {preflight && (
              <div className="space-y-2 rounded-xl border p-3">
                <div className="flex items-center justify-between">
                  <p className="font-semibold">Preflight</p>
                  <Badge
                    className={
                      preflight.ready
                        ? "bg-success-muted text-success-muted-foreground"
                        : "bg-critical-muted text-critical-muted-foreground"
                    }
                  >
                    {preflight.ready ? "Ready" : "Action required"}
                  </Badge>
                </div>
                {preflight.checks.map((check) => (
                  <p className="flex items-start gap-2 text-sm" key={check.key}>
                    {check.passed ? (
                      <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" />
                    ) : (
                      <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" />
                    )}
                    <span>
                      {check.key.replace(/_/g, " ")}:{" "}
                      {check.passed
                        ? "passed"
                        : (check.reason_code ?? "action required").replace(
                            /_/g,
                            " ",
                          )}
                    </span>
                  </p>
                ))}
              </div>
            )}

            {(preflightMutation.isError || onboardMutation.isError) && (
              <Alert variant="destructive">
                <AlertDescription>
                  Clinic onboarding did not complete. Review the preflight and
                  try again.
                </AlertDescription>
              </Alert>
            )}
            <DialogFooter>
              <Button
                disabled={onboardMutation.isPending}
                onClick={() => setOpen(false)}
                type="button"
                variant="outline"
              >
                Cancel
              </Button>
              <Button
                disabled={
                  preflightMutation.isPending ||
                  onboardMutation.isPending ||
                  !form.code ||
                  !form.slug ||
                  !form.name ||
                  !form.initial_staff[0]?.email ||
                  !form.initial_staff[0]?.full_name ||
                  form.supported_languages.length === 0
                }
                type="submit"
              >
                {(preflightMutation.isPending || onboardMutation.isPending) && (
                  <LoaderCircle className="animate-spin" />
                )}
                {preflight?.ready ? "Create clinic" : "Run preflight"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  )
}

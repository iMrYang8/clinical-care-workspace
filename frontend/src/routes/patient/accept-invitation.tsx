import { useMutation } from "@tanstack/react-query"
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router"
import { Eye, EyeOff, LoaderCircle, ShieldCheck } from "lucide-react"
import { type FormEvent, useEffect, useState } from "react"

import { AuthLayout } from "@/components/Common/AuthLayout"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  type PatientInvitationPreview,
  patientInvitationApi,
} from "@/features/api"

export const Route = createFileRoute("/patient/accept-invitation")({
  component: PatientInvitationPage,
  head: () => ({ meta: [{ title: "Patient invitation · Nightingale" }] }),
})

function invitationToken(): string {
  try {
    return decodeURIComponent(window.location.hash.slice(1))
  } catch {
    return ""
  }
}

function PatientInvitationPage() {
  const navigate = useNavigate()
  const [token, setToken] = useState("")
  const [email, setEmail] = useState("")
  const [fullName, setFullName] = useState("")
  const [password, setPassword] = useState("")
  const [confirmPassword, setConfirmPassword] = useState("")
  const [showPassword, setShowPassword] = useState(false)
  const [preview, setPreview] = useState<PatientInvitationPreview | null>(null)

  useEffect(() => {
    const value = invitationToken()
    setToken(value)
    if (window.location.hash)
      window.history.replaceState(null, "", window.location.pathname)
  }, [])

  const previewMutation = useMutation({
    mutationFn: () =>
      patientInvitationApi.preview({
        token,
        email: email.trim().toLowerCase(),
      }),
    onSuccess: setPreview,
  })
  const acceptMutation = useMutation({
    mutationFn: () =>
      patientInvitationApi.accept({
        token,
        email: email.trim().toLowerCase(),
        password,
        full_name: fullName || undefined,
      }),
    onSuccess: () => navigate({ to: "/patient/my-care" }),
  })

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!preview) {
      previewMutation.mutate()
      return
    }
    if (!preview.account_exists && password.length < 16) return
    if (!preview.account_exists && password !== confirmPassword) return
    acceptMutation.mutate()
  }

  return (
    <AuthLayout>
      <form
        className="space-y-4 rounded-2xl border bg-card p-6 shadow-sm"
        onSubmit={submit}
      >
        <div>
          <p className="text-xs font-bold uppercase tracking-[0.18em] text-primary">
            Patient access
          </p>
          <h1 className="mt-1 font-serif text-3xl font-semibold">
            Access care information shared with you
          </h1>
          <p className="mt-2 text-sm leading-6 text-muted-foreground">
            Confirm the email address that received the invitation. Your clinic
            controls what clinical information is approved for sharing.
          </p>
        </div>
        <div className="space-y-2">
          <Label htmlFor="patient-invite-email">Email</Label>
          <Input
            id="patient-invite-email"
            type="email"
            required
            value={email}
            onChange={(event) => {
              setEmail(event.target.value)
              setPreview(null)
            }}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="patient-invite-token">Invitation code</Label>
          <Input
            id="patient-invite-token"
            required
            minLength={64}
            value={token}
            onChange={(event) => {
              setToken(event.target.value)
              setPreview(null)
            }}
          />
        </div>
        {preview && (
          <Alert className="border-success/40 bg-success-muted text-success-muted-foreground">
            <ShieldCheck className="size-4" />
            <AlertTitle>{preview.clinic_name}</AlertTitle>
            <AlertDescription>
              Access invitation for {preview.patient_display_name}.
            </AlertDescription>
          </Alert>
        )}
        {preview && !preview.account_exists && (
          <div className="space-y-2">
            <Label htmlFor="patient-full-name">Your name</Label>
            <Input
              id="patient-full-name"
              maxLength={255}
              value={fullName}
              onChange={(event) => setFullName(event.target.value)}
            />
          </div>
        )}
        {preview && (
          <div className="space-y-2">
            <Label htmlFor="patient-invite-password">
              {preview.account_exists
                ? "Existing account password"
                : "Create password"}
            </Label>
            <div className="relative">
              <Input
                id="patient-invite-password"
                type={showPassword ? "text" : "password"}
                required
                minLength={preview.account_exists ? 1 : 16}
                maxLength={200}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
              <button
                type="button"
                className="absolute right-2 top-2 text-muted-foreground"
                onClick={() => setShowPassword((value) => !value)}
                aria-label="Show or hide password"
              >
                {showPassword ? (
                  <EyeOff className="size-5" />
                ) : (
                  <Eye className="size-5" />
                )}
              </button>
            </div>
            {!preview.account_exists && (
              <p className="text-xs text-muted-foreground">
                Use a passphrase of 16–200 characters.
              </p>
            )}
          </div>
        )}
        {preview && !preview.account_exists && (
          <div className="space-y-2">
            <Label htmlFor="patient-invite-confirm">Confirm password</Label>
            <Input
              id="patient-invite-confirm"
              type="password"
              required
              minLength={16}
              maxLength={200}
              value={confirmPassword}
              onChange={(event) => setConfirmPassword(event.target.value)}
            />
            {confirmPassword && password !== confirmPassword && (
              <p className="text-sm text-critical-muted-foreground">
                Passwords do not match.
              </p>
            )}
          </div>
        )}
        {(previewMutation.isError || acceptMutation.isError) && (
          <Alert variant="destructive">
            <AlertDescription>
              The invitation could not be verified. Check the email and code, or
              request a new invitation.
            </AlertDescription>
          </Alert>
        )}
        <Button
          className="w-full min-h-11"
          disabled={
            previewMutation.isPending ||
            acceptMutation.isPending ||
            Boolean(
              preview &&
                !preview.account_exists &&
                (password.length < 16 || password !== confirmPassword),
            )
          }
          type="submit"
        >
          {(previewMutation.isPending || acceptMutation.isPending) && (
            <LoaderCircle className="animate-spin" />
          )}
          {preview ? "Activate patient access" : "Continue"}
        </Button>
        <Button asChild variant="ghost" className="w-full">
          <Link to="/patient/login">Return to patient sign in</Link>
        </Button>
      </form>
    </AuthLayout>
  )
}

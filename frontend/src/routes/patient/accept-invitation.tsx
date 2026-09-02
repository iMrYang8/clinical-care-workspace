import { useMutation } from "@tanstack/react-query"
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router"
import {
  Eye,
  EyeOff,
  KeyRound,
  LoaderCircle,
  Mail,
  MessageSquareText,
  ShieldCheck,
} from "lucide-react"
import { type FormEvent, useEffect, useState } from "react"

import { AuthLayout } from "@/components/Common/AuthLayout"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  type PatientAccessChallenge,
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
  const [accessMode, setAccessMode] = useState<"phone" | "email">("phone")
  const [claimCode, setClaimCode] = useState("")
  const [phone, setPhone] = useState("")
  const [otp, setOtp] = useState("")
  const [challenge, setChallenge] = useState<PatientAccessChallenge | null>(
    null,
  )
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    const value = invitationToken()
    setToken(value)
    if (window.location.hash)
      window.history.replaceState(null, "", window.location.pathname)
  }, [])

  useEffect(() => {
    if (!challenge) return
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [challenge])

  const resendSeconds = challenge
    ? Math.max(
        0,
        Math.ceil(
          (new Date(challenge.resend_available_at).getTime() - now) / 1_000,
        ),
      )
    : 0

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
  const requestOtpMutation = useMutation({
    mutationFn: () =>
      patientInvitationApi.requestEnrollmentOtp({
        invitation_token: token,
        claim_code: claimCode.trim(),
        phone: phone.trim(),
      }),
    onSuccess: (value) => {
      setChallenge(value)
      setOtp("")
    },
  })
  const acceptPhoneMutation = useMutation({
    mutationFn: () => {
      if (!challenge) throw new Error("Verification challenge is missing")
      return patientInvitationApi.acceptPhone({
        challenge_token: challenge.challenge_token,
        otp: otp.trim(),
      })
    },
    onSuccess: () => navigate({ to: "/patient/my-care" }),
  })

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (accessMode === "phone") {
      if (challenge) acceptPhoneMutation.mutate()
      else requestOtpMutation.mutate()
      return
    }
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
            Claim your invitation with your phone and a one-time code. Your
            clinic controls what clinical information is approved for sharing.
          </p>
        </div>
        <div className="grid grid-cols-2 gap-2 rounded-xl border bg-muted/30 p-1">
          <Button
            onClick={() => {
              setAccessMode("phone")
              setPreview(null)
            }}
            type="button"
            variant={accessMode === "phone" ? "default" : "ghost"}
          >
            <MessageSquareText /> Phone and claim code
          </Button>
          <Button
            onClick={() => {
              setAccessMode("email")
              setChallenge(null)
            }}
            type="button"
            variant={accessMode === "email" ? "default" : "ghost"}
          >
            <Mail /> Existing email flow
          </Button>
        </div>
        {accessMode === "email" && (
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
        )}
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
        {accessMode === "phone" && !challenge && (
          <>
            <div className="space-y-2">
              <Label htmlFor="patient-claim-code">Patient claim code</Label>
              <Input
                autoComplete="one-time-code"
                id="patient-claim-code"
                onChange={(event) => setClaimCode(event.target.value)}
                required
                value={claimCode}
              />
              <p className="text-xs leading-5 text-muted-foreground">
                This patient-specific code is separate from the invitation link,
                is single-use, and expires after seven days.
              </p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="patient-enrollment-phone">Registered phone</Label>
              <Input
                autoComplete="tel"
                id="patient-enrollment-phone"
                inputMode="tel"
                onChange={(event) => setPhone(event.target.value)}
                placeholder="+65 …"
                required
                type="tel"
                value={phone}
              />
              <p className="text-xs leading-5 text-muted-foreground">
                This must match the encrypted phone registered by your clinic.
                The one-time code is delivered only to that registered number;
                the patient-specific claim code prevents a shared number from
                selecting the wrong record.
              </p>
            </div>
          </>
        )}
        {accessMode === "phone" && challenge && (
          <>
            <Alert className="border-success/40 bg-success-muted text-success-muted-foreground">
              <KeyRound className="size-4" />
              <AlertTitle>Code sent to {challenge.masked_phone}</AlertTitle>
              <AlertDescription>
                {challenge.attempts_remaining} attempts remain. The code expires
                after 10 minutes.
              </AlertDescription>
            </Alert>
            <div className="space-y-2">
              <Label htmlFor="patient-enrollment-otp">One-time code</Label>
              <Input
                autoComplete="one-time-code"
                autoFocus
                id="patient-enrollment-otp"
                inputMode="numeric"
                maxLength={8}
                onChange={(event) =>
                  setOtp(event.target.value.replace(/\D/g, ""))
                }
                pattern="[0-9]{6,8}"
                required
                value={otp}
              />
            </div>
          </>
        )}
        {accessMode === "email" && preview && (
          <Alert className="border-success/40 bg-success-muted text-success-muted-foreground">
            <ShieldCheck className="size-4" />
            <AlertTitle>{preview.clinic_name}</AlertTitle>
            <AlertDescription>
              Access invitation for {preview.patient_display_name}.
            </AlertDescription>
          </Alert>
        )}
        {accessMode === "email" && preview && !preview.account_exists && (
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
        {accessMode === "email" && preview && (
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
        {accessMode === "email" && preview && !preview.account_exists && (
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
        {(previewMutation.isError ||
          acceptMutation.isError ||
          requestOtpMutation.isError ||
          acceptPhoneMutation.isError) && (
          <Alert variant="destructive">
            <AlertDescription>
              The invitation could not be verified. Check the invitation, claim
              code, phone, and one-time code, or request a new invitation.
            </AlertDescription>
          </Alert>
        )}
        <Button
          className="w-full min-h-11"
          disabled={
            previewMutation.isPending ||
            acceptMutation.isPending ||
            requestOtpMutation.isPending ||
            acceptPhoneMutation.isPending ||
            Boolean(
              accessMode === "email" &&
                preview &&
                !preview.account_exists &&
                (password.length < 16 || password !== confirmPassword),
            ) ||
            Boolean(
              accessMode === "phone" &&
                (challenge
                  ? otp.length < 6
                  : !claimCode.trim() || !phone.trim() || !token.trim()),
            )
          }
          type="submit"
        >
          {(previewMutation.isPending ||
            acceptMutation.isPending ||
            requestOtpMutation.isPending ||
            acceptPhoneMutation.isPending) && (
            <LoaderCircle className="animate-spin" />
          )}
          {accessMode === "phone"
            ? challenge
              ? "Verify and activate My Care"
              : "Send one-time code"
            : preview
              ? "Activate patient access"
              : "Continue"}
        </Button>
        {accessMode === "phone" && challenge && (
          <Button
            className="w-full"
            disabled={requestOtpMutation.isPending || resendSeconds > 0}
            onClick={() => requestOtpMutation.mutate()}
            type="button"
            variant="outline"
          >
            {resendSeconds > 0
              ? `Send a new code in ${resendSeconds}s`
              : "Send a new one-time code"}
          </Button>
        )}
        <Button asChild variant="ghost" className="w-full">
          <Link to="/patient/login">Return to patient sign in</Link>
        </Button>
      </form>
    </AuthLayout>
  )
}

import { useMutation } from "@tanstack/react-query"
import { Link, useNavigate } from "@tanstack/react-router"
import {
  KeyRound,
  LoaderCircle,
  MessageSquareText,
  RotateCcw,
} from "lucide-react"
import { type FormEvent, useEffect, useMemo, useState } from "react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  apiErrorMessage,
  type PatientAccessChallenge,
  patientAccessApi,
} from "@/features/api"

export function normalizePortalId(value: string): string {
  return value.trim().toUpperCase()
}

function PatientOtpLoginForm({ onUseEmail }: { onUseEmail?: () => void }) {
  const navigate = useNavigate()
  const [portalId, setPortalId] = useState("")
  const [otp, setOtp] = useState("")
  const [challenge, setChallenge] = useState<PatientAccessChallenge | null>(
    null,
  )
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!challenge) return
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [challenge])

  const requestMutation = useMutation({
    mutationFn: () =>
      patientAccessApi.requestLoginOtp({
        portal_id: normalizePortalId(portalId),
      }),
    onSuccess: (value) => {
      setChallenge(value)
      setOtp("")
    },
  })
  const verifyMutation = useMutation({
    mutationFn: () => {
      if (!challenge) throw new Error("Verification challenge is missing")
      return patientAccessApi.verifyOtp({
        challenge_token: challenge.challenge_token,
        otp: otp.trim(),
      })
    },
    onSuccess: () => navigate({ to: "/patient/my-care", replace: true }),
  })
  const destination = challenge?.masked_phone ?? "your registered phone"
  const error = requestMutation.error ?? verifyMutation.error
  const expiresAt = useMemo(
    () =>
      challenge?.expires_at
        ? new Date(challenge.expires_at).toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
          })
        : null,
    [challenge?.expires_at],
  )
  const resendSeconds = challenge
    ? Math.max(
        0,
        Math.ceil(
          (new Date(challenge.resend_available_at).getTime() - now) / 1_000,
        ),
      )
    : 0

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (challenge) verifyMutation.mutate()
    else requestMutation.mutate()
  }

  return (
    <div className="space-y-7">
      <div className="space-y-3">
        <p className="text-sm font-semibold uppercase tracking-[0.18em] text-primary">
          Patient access
        </p>
        <h1 className="font-serif text-4xl font-semibold tracking-tight text-foreground">
          Sign in to My Care
        </h1>
        <p className="leading-7 text-muted-foreground">
          Use your portal ID and the one-time code sent to your registered
          phone. An email address and password are not required.
        </p>
      </div>

      {error && (
        <Alert role="alert" variant="destructive">
          <AlertTitle>Sign-in did not complete</AlertTitle>
          <AlertDescription>{apiErrorMessage(error)}</AlertDescription>
        </Alert>
      )}

      <form
        className="space-y-4 rounded-2xl border border-border bg-card p-5 text-card-foreground shadow-sm"
        data-testid="patient-otp-login-form"
        onSubmit={submit}
      >
        <div>
          <h2 className="flex items-center gap-2 font-serif text-xl font-semibold">
            <MessageSquareText className="size-5 text-primary" /> Phone
            verification
          </h2>
          <p className="mt-1 text-sm leading-6 text-muted-foreground">
            Codes expire after 10 minutes. Five verification attempts are
            allowed.
          </p>
        </div>

        <div className="space-y-2">
          <Label htmlFor="patient-portal-id">Portal ID</Label>
          <Input
            autoCapitalize="characters"
            autoComplete="username"
            disabled={Boolean(challenge)}
            id="patient-portal-id"
            onChange={(event) => setPortalId(event.target.value)}
            placeholder="MYCARE-…"
            required
            value={portalId}
          />
        </div>

        {challenge && (
          <>
            <Alert className="border-primary/30 bg-primary/10">
              <KeyRound className="size-4" />
              <AlertTitle>Enter the code sent to {destination}</AlertTitle>
              <AlertDescription>
                {challenge.attempts_remaining} attempts remain
                {expiresAt ? ` · code expires at ${expiresAt}` : ""}.
              </AlertDescription>
            </Alert>
            <div className="space-y-2">
              <Label htmlFor="patient-login-otp">One-time code</Label>
              <Input
                autoComplete="one-time-code"
                autoFocus
                id="patient-login-otp"
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

        <Button
          className="w-full"
          disabled={
            requestMutation.isPending ||
            verifyMutation.isPending ||
            !portalId.trim() ||
            Boolean(challenge && otp.length < 6)
          }
          type="submit"
        >
          {(requestMutation.isPending || verifyMutation.isPending) && (
            <LoaderCircle className="animate-spin" />
          )}
          {challenge ? "Verify and sign in" : "Send one-time code"}
        </Button>

        {challenge && (
          <Button
            className="w-full"
            disabled={requestMutation.isPending || resendSeconds > 0}
            onClick={() => requestMutation.mutate()}
            type="button"
            variant="outline"
          >
            <RotateCcw />{" "}
            {resendSeconds > 0
              ? `Send a new code in ${resendSeconds}s`
              : "Send a new code"}
          </Button>
        )}
      </form>

      <p className="text-center text-sm text-muted-foreground">
        Have an older email account?{" "}
        <button
          className="font-semibold text-primary underline-offset-4 hover:underline"
          onClick={onUseEmail}
          type="button"
        >
          Use email and password
        </button>
      </p>
      <p className="text-center text-sm text-muted-foreground">
        Part of a clinical team?{" "}
        <Link
          className="font-semibold text-primary underline-offset-4 hover:underline"
          to="/login"
        >
          Clinical sign in
        </Link>
      </p>
    </div>
  )
}

export default PatientOtpLoginForm

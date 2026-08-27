import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation } from "@tanstack/react-query"
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router"
import { Eye, EyeOff, LoaderCircle, ShieldCheck } from "lucide-react"
import { useEffect, useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { normalizeEmail } from "@/components/Auth/ClinicLoginForm"
import { AuthLayout } from "@/components/Common/AuthLayout"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { apiErrorMessage, authApi } from "@/features/api"

export const Route = createFileRoute("/accept-invitation")({
  component: AcceptInvitation,
  head: () => ({ meta: [{ title: "Accept invitation · Nightingale" }] }),
})

export const invitationAcceptanceSchema = z
  .object({
    email: z
      .string()
      .trim()
      .toLowerCase()
      .email("Enter the email address that received the invitation."),
    token: z
      .string()
      .trim()
      .min(64, "Enter the complete one-time code from your invitation.")
      .max(512, "The one-time code is too long."),
    fullName: z
      .string()
      .max(200, "Display name must be 200 characters or fewer."),
    password: z
      .string()
      .min(16, "Use at least 16 characters. Long passphrases are supported.")
      .max(200, "Password must be 200 characters or fewer."),
    confirmPassword: z.string().min(1, "Enter the password again."),
  })
  .refine((values) => values.password === values.confirmPassword, {
    message: "Passwords do not match.",
    path: ["confirmPassword"],
  })

type InvitationAcceptanceValues = z.infer<typeof invitationAcceptanceSchema>

function invitationCodeFromFragment(): string {
  if (!window.location.hash) return ""
  try {
    return decodeURIComponent(window.location.hash.slice(1))
  } catch {
    return ""
  }
}

function AcceptInvitation() {
  const navigate = useNavigate()
  const [showPassword, setShowPassword] = useState(false)
  const {
    formState: { errors },
    handleSubmit,
    register,
    setValue,
  } = useForm<InvitationAcceptanceValues>({
    resolver: zodResolver(invitationAcceptanceSchema),
    defaultValues: {
      email: "",
      token: "",
      fullName: "",
      password: "",
      confirmPassword: "",
    },
  })

  useEffect(() => {
    const fragmentCode = invitationCodeFromFragment()
    if (fragmentCode) setValue("token", fragmentCode)
    if (window.location.hash) {
      // URL fragments never reach the server. Remove the one-time code from
      // browser history after it has been copied into form memory.
      window.history.replaceState(null, "", window.location.pathname)
    }
  }, [setValue])

  const acceptance = useMutation({
    mutationFn: authApi.acceptInvitation,
    onSuccess: async () => {
      await navigate({ to: "/login", replace: true })
    },
  })

  const emailField = register("email")

  return (
    <AuthLayout>
      <div className="space-y-6">
        <div className="space-y-3">
          <span className="grid size-12 place-items-center rounded-2xl bg-primary text-primary-foreground">
            <ShieldCheck aria-hidden="true" className="size-6" />
          </span>
          <h1 className="font-serif text-4xl font-semibold text-foreground">
            Join your clinic workspace
          </h1>
          <p className="leading-7 text-muted-foreground">
            Enter the email address and one-time code from your invitation, then
            create a password for your Nightingale account.
          </p>
        </div>

        {acceptance.isError && (
          <Alert role="alert" variant="destructive">
            <AlertTitle>Invitation was not accepted</AlertTitle>
            <AlertDescription>
              {apiErrorMessage(acceptance.error)}
            </AlertDescription>
          </Alert>
        )}

        <form
          className="space-y-4"
          data-testid="invitation-acceptance-form"
          noValidate
          onSubmit={handleSubmit((values) => {
            acceptance.mutate({
              email: normalizeEmail(values.email),
              token: values.token.trim(),
              password: values.password,
              full_name: values.fullName.trim() || null,
            })
          })}
        >
          <div className="space-y-2">
            <Label htmlFor="invitation-email">Invited email</Label>
            <Input
              {...emailField}
              aria-describedby={
                errors.email ? "invitation-email-error" : undefined
              }
              aria-invalid={Boolean(errors.email)}
              autoCapitalize="none"
              autoComplete="email"
              id="invitation-email"
              onBlur={(event) => {
                void emailField.onBlur(event)
                setValue("email", normalizeEmail(event.target.value), {
                  shouldDirty: true,
                  shouldValidate: true,
                })
              }}
              type="email"
            />
            {errors.email && (
              <p
                className="text-sm text-destructive"
                id="invitation-email-error"
              >
                {errors.email.message}
              </p>
            )}
          </div>

          <div className="space-y-2">
            <Label htmlFor="invitation-code">One-time code</Label>
            <Input
              {...register("token")}
              aria-describedby={
                errors.token ? "invitation-code-error" : undefined
              }
              aria-invalid={Boolean(errors.token)}
              autoComplete="off"
              id="invitation-code"
              spellCheck={false}
            />
            {errors.token && (
              <p
                className="text-sm text-destructive"
                id="invitation-code-error"
              >
                {errors.token.message}
              </p>
            )}
          </div>

          <div className="space-y-2">
            <Label htmlFor="invitation-name">Display name</Label>
            <Input
              {...register("fullName")}
              aria-describedby={
                errors.fullName ? "invitation-name-error" : undefined
              }
              aria-invalid={Boolean(errors.fullName)}
              autoComplete="name"
              id="invitation-name"
            />
            {errors.fullName && (
              <p
                className="text-sm text-destructive"
                id="invitation-name-error"
              >
                {errors.fullName.message}
              </p>
            )}
          </div>

          <div className="space-y-2">
            <Label htmlFor="invitation-password">New password</Label>
            <div className="relative">
              <Input
                {...register("password")}
                aria-describedby="invitation-password-help invitation-password-error"
                aria-invalid={Boolean(errors.password)}
                autoComplete="new-password"
                className="pr-11"
                id="invitation-password"
                type={showPassword ? "text" : "password"}
              />
              <Button
                aria-label={showPassword ? "Hide passwords" : "Show passwords"}
                className="absolute right-0 top-0"
                onClick={() => setShowPassword((visible) => !visible)}
                size="icon"
                type="button"
                variant="ghost"
              >
                {showPassword ? (
                  <EyeOff aria-hidden="true" />
                ) : (
                  <Eye aria-hidden="true" />
                )}
              </Button>
            </div>
            <p
              className="text-sm text-muted-foreground"
              id="invitation-password-help"
            >
              Use 16–200 characters. Long passphrases are supported.
            </p>
            {errors.password && (
              <p
                className="text-sm text-destructive"
                id="invitation-password-error"
              >
                {errors.password.message}
              </p>
            )}
          </div>

          <div className="space-y-2">
            <Label htmlFor="invitation-password-confirm">
              Confirm password
            </Label>
            <Input
              {...register("confirmPassword")}
              aria-describedby={
                errors.confirmPassword
                  ? "invitation-password-confirm-error"
                  : undefined
              }
              aria-invalid={Boolean(errors.confirmPassword)}
              autoComplete="new-password"
              id="invitation-password-confirm"
              type={showPassword ? "text" : "password"}
            />
            {errors.confirmPassword && (
              <p
                className="text-sm text-destructive"
                id="invitation-password-confirm-error"
              >
                {errors.confirmPassword.message}
              </p>
            )}
          </div>

          <Button
            className="w-full"
            disabled={acceptance.isPending}
            type="submit"
          >
            {acceptance.isPending && (
              <LoaderCircle aria-hidden="true" className="animate-spin" />
            )}
            Activate account
          </Button>
        </form>

        <Button asChild className="w-full" variant="ghost">
          <Link to="/login">Back to clinical sign in</Link>
        </Button>
      </div>
    </AuthLayout>
  )
}

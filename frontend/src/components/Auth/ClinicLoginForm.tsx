import { zodResolver } from "@hookform/resolvers/zod"
import { Link } from "@tanstack/react-router"
import { Eye, EyeOff, LoaderCircle } from "lucide-react"
import { useState } from "react"
import { useForm } from "react-hook-form"
import * as z from "zod"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { apiErrorMessage } from "@/features/api"
import useAuth from "@/hooks/useAuth"

export const CLINIC_CODE_PATTERN = /^[A-Z]{3,12}$/

export const clinicLoginSchema = z.object({
  clinicCode: z
    .string()
    .toUpperCase()
    .regex(
      CLINIC_CODE_PATTERN,
      "Use 3–12 English letters. Your clinic code is shown in uppercase.",
    ),
  email: z.string().trim().toLowerCase().email("Enter a valid email address."),
  password: z
    .string()
    .min(1, "Enter your password.")
    .max(200, "Password must be 200 characters or fewer."),
})

export type ClinicLoginValues = z.infer<typeof clinicLoginSchema>

export function normalizeClinicCode(value: string): string {
  return value.toUpperCase()
}

export function normalizeEmail(value: string): string {
  return value.trim().toLowerCase()
}

export function ClinicLoginForm({
  portal,
}: {
  portal: "clinical" | "patient"
}) {
  const { passwordLoginMutation } = useAuth({ loadSession: false })
  const [showPassword, setShowPassword] = useState(false)
  const {
    formState: { errors },
    handleSubmit,
    register,
    setValue,
  } = useForm<ClinicLoginValues>({
    resolver: zodResolver(clinicLoginSchema),
    defaultValues: { clinicCode: "", email: "", password: "" },
  })

  const clinicCodeField = register("clinicCode")
  const emailField = register("email")
  const isPatientPortal = portal === "patient"

  return (
    <div className="space-y-7">
      <div className="space-y-3">
        <p className="text-sm font-semibold uppercase tracking-[0.18em] text-primary">
          {isPatientPortal ? "Patient access" : "Clinical team access"}
        </p>
        <h1 className="font-serif text-4xl font-semibold tracking-tight text-foreground">
          {isPatientPortal ? "Sign in to My Care" : "Sign in to Nightingale"}
        </h1>
        <p className="leading-7 text-muted-foreground">
          {isPatientPortal
            ? "Review care information shared with you and send updates to your care team."
            : "Access patient priorities, care documentation, and team collaboration for your clinic."}
        </p>
      </div>

      {passwordLoginMutation.isError && (
        <Alert role="alert" variant="destructive">
          <AlertTitle>Sign-in did not complete</AlertTitle>
          <AlertDescription>
            {apiErrorMessage(passwordLoginMutation.error)}
          </AlertDescription>
        </Alert>
      )}

      <form
        className="space-y-4 rounded-2xl border border-border bg-card p-5 text-card-foreground shadow-sm"
        data-testid={`${portal}-login-form`}
        noValidate
        onSubmit={handleSubmit((values) => {
          passwordLoginMutation.mutate({
            clinicCode: normalizeClinicCode(values.clinicCode),
            email: normalizeEmail(values.email),
            password: values.password,
          })
        })}
      >
        <div>
          <h2 className="font-serif text-xl font-semibold">Clinic account</h2>
          <p className="mt-1 text-sm leading-6 text-muted-foreground">
            Enter the clinic code supplied by your clinic administrator.
          </p>
        </div>

        <div className="space-y-2">
          <Label htmlFor={`${portal}-clinic-code`}>Clinic code</Label>
          <Input
            {...clinicCodeField}
            aria-describedby={
              errors.clinicCode ? `${portal}-clinic-code-error` : undefined
            }
            aria-invalid={Boolean(errors.clinicCode)}
            autoCapitalize="characters"
            autoComplete="organization"
            id={`${portal}-clinic-code`}
            onChange={(event) => {
              event.target.value = normalizeClinicCode(event.target.value)
              void clinicCodeField.onChange(event)
            }}
            pattern="[A-Za-z]{3,12}"
            placeholder="NIGHTINGALE"
            spellCheck={false}
          />
          {errors.clinicCode && (
            <p
              className="text-sm text-destructive"
              id={`${portal}-clinic-code-error`}
            >
              {errors.clinicCode.message}
            </p>
          )}
        </div>

        <div className="space-y-2">
          <Label htmlFor={`${portal}-login-email`}>Email</Label>
          <Input
            {...emailField}
            aria-describedby={
              errors.email ? `${portal}-login-email-error` : undefined
            }
            aria-invalid={Boolean(errors.email)}
            autoCapitalize="none"
            autoComplete="username"
            id={`${portal}-login-email`}
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
              id={`${portal}-login-email-error`}
            >
              {errors.email.message}
            </p>
          )}
        </div>

        <div className="space-y-2">
          <Label htmlFor={`${portal}-login-password`}>Password</Label>
          <div className="relative">
            <Input
              {...register("password")}
              aria-describedby={
                errors.password ? `${portal}-login-password-error` : undefined
              }
              aria-invalid={Boolean(errors.password)}
              autoComplete="current-password"
              className="pr-11"
              id={`${portal}-login-password`}
              type={showPassword ? "text" : "password"}
            />
            <Button
              aria-label={showPassword ? "Hide password" : "Show password"}
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
          {errors.password && (
            <p
              className="text-sm text-destructive"
              id={`${portal}-login-password-error`}
            >
              {errors.password.message}
            </p>
          )}
        </div>

        <Button
          className="w-full"
          disabled={passwordLoginMutation.isPending}
          type="submit"
        >
          {passwordLoginMutation.isPending && (
            <LoaderCircle aria-hidden="true" className="animate-spin" />
          )}
          {isPatientPortal ? "Sign in to My Care" : "Sign in to clinic"}
        </Button>
      </form>

      <p className="text-center text-sm text-muted-foreground">
        {isPatientPortal
          ? "Part of a clinical team? "
          : "Looking for My Care? "}
        <Link
          className="font-semibold text-primary underline-offset-4 hover:underline"
          to={isPatientPortal ? "/login" : "/patient/login"}
        >
          {isPatientPortal ? "Clinical sign in" : "Patient sign in"}
        </Link>
      </p>
    </div>
  )
}

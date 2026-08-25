import { createFileRoute, redirect } from "@tanstack/react-router"
import {
  ArrowRight,
  HeartHandshake,
  LoaderCircle,
  ShieldCheck,
  Stethoscope,
  UserRound,
} from "lucide-react"
import { useState } from "react"

import { AuthLayout } from "@/components/Common/AuthLayout"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import type { DemoPersona } from "@/features/api"
import useAuth, { isLoggedIn } from "@/hooks/useAuth"

export const Route = createFileRoute("/login")({
  beforeLoad: async () => {
    if (await isLoggedIn()) throw redirect({ to: "/" })
  },
  component: Login,
  head: () => ({
    meta: [{ title: "Sign in · Nightingale" }],
  }),
})

const personas = [
  {
    persona: "staff" as const,
    title: "Care staff",
    description: "Review Glance, write the staff section, and collaborate.",
    icon: HeartHandshake,
    color: "border-blue-200 bg-blue-50 text-blue-950",
    iconColor: "bg-blue-600 text-white",
  },
  {
    persona: "clinician" as const,
    title: "Clinician",
    description: "Write clinical notes, review sources, and resolve comments.",
    icon: Stethoscope,
    color: "border-teal-200 bg-teal-50 text-teal-950",
    iconColor: "bg-teal-700 text-white",
  },
  {
    persona: "patient" as const,
    title: "Patient",
    description: "See only the patient-facing care view and add an insight.",
    icon: UserRound,
    color: "border-amber-200 bg-amber-50 text-amber-950",
    iconColor: "bg-amber-500 text-white",
  },
  {
    persona: "admin" as const,
    title: "Clinic admin",
    description: "See membership and audit boundaries—never clinical text.",
    icon: ShieldCheck,
    color: "border-slate-200 bg-slate-50 text-slate-950",
    iconColor: "bg-slate-700 text-white",
  },
]

function Login() {
  // The route guard already performed the anonymous /me probe and secure
  // cleanup. Do not launch a second unauthenticated /me query from the form.
  const { loginMutation, passwordLoginMutation } = useAuth({
    loadSession: false,
  })
  const [clinicId, setClinicId] = useState("")
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")

  const signIn = (persona: DemoPersona) => loginMutation.mutate(persona)

  return (
    <AuthLayout>
      <div className="space-y-7">
        <div className="space-y-3">
          <Badge className="bg-teal-100 text-teal-800 hover:bg-teal-100">
            72-hour synthetic demo
          </Badge>
          <h2 className="font-serif text-4xl font-semibold tracking-tight text-slate-950">
            Enter the care workspace
          </h2>
          <p className="leading-7 text-slate-600">
            Choose one server-defined persona. The API resolves its clinic and
            role from the signed membership—there is no client-side role switch.
          </p>
        </div>

        {(loginMutation.isError || passwordLoginMutation.isError) && (
          <Alert className="border-red-200 bg-red-50 text-red-900" role="alert">
            <AlertTitle>Sign-in did not complete</AlertTitle>
            <AlertDescription>
              {(passwordLoginMutation.error ?? loginMutation.error)?.message}
            </AlertDescription>
          </Alert>
        )}

        <form
          className="space-y-4 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm"
          onSubmit={(event) => {
            event.preventDefault()
            passwordLoginMutation.mutate({
              clinicId: clinicId.trim(),
              email: email.trim(),
              password,
            })
          }}
        >
          <div>
            <h3 className="font-serif text-xl font-semibold text-slate-950">
              Clinic account
            </h3>
            <p className="mt-1 text-sm leading-6 text-slate-600">
              Use the clinic identifier supplied by your administrator. Your
              browser session is held only in a secure HttpOnly cookie.
            </p>
          </div>
          <div className="space-y-2">
            <Label htmlFor="clinic-id">Clinic ID</Label>
            <Input
              autoComplete="organization"
              id="clinic-id"
              onChange={(event) => setClinicId(event.target.value)}
              placeholder="00000000-0000-0000-0000-000000000000"
              required
              spellCheck={false}
              value={clinicId}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="login-email">Email</Label>
            <Input
              autoComplete="username"
              id="login-email"
              onChange={(event) => setEmail(event.target.value)}
              required
              type="email"
              value={email}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="login-password">Password</Label>
            <Input
              autoComplete="current-password"
              id="login-password"
              onChange={(event) => setPassword(event.target.value)}
              required
              type="password"
              value={password}
            />
          </div>
          <Button
            className="w-full"
            disabled={passwordLoginMutation.isPending}
            type="submit"
          >
            {passwordLoginMutation.isPending && (
              <LoaderCircle aria-hidden="true" className="animate-spin" />
            )}
            Sign in to clinic
          </Button>
        </form>

        <div className="flex items-center gap-3">
          <span className="h-px flex-1 bg-slate-200" />
          <span className="text-xs font-semibold uppercase tracking-wider text-slate-500">
            Development demo
          </span>
          <span className="h-px flex-1 bg-slate-200" />
        </div>

        <fieldset className="grid gap-3">
          <legend className="sr-only">Development demo personas</legend>
          {personas.map(
            ({ persona, title, description, icon: Icon, color, iconColor }) => (
              <Card
                className={`border shadow-none transition hover:-translate-y-0.5 hover:shadow-sm ${color}`}
                key={persona}
              >
                <CardContent className="flex items-center gap-4 p-4">
                  <span
                    className={`grid size-11 shrink-0 place-items-center rounded-2xl ${iconColor}`}
                  >
                    <Icon aria-hidden="true" className="size-5" />
                  </span>
                  <span className="min-w-0 flex-1 text-left">
                    <span className="block font-semibold">{title}</span>
                    <span className="block text-sm leading-5 opacity-75">
                      {description}
                    </span>
                  </span>
                  <Button
                    aria-label={`Continue as ${title}`}
                    className="size-11 shrink-0 rounded-full"
                    disabled={loginMutation.isPending}
                    onClick={() => signIn(persona)}
                    size="icon"
                    variant="ghost"
                  >
                    {loginMutation.isPending &&
                    loginMutation.variables === persona ? (
                      <LoaderCircle
                        aria-hidden="true"
                        className="animate-spin"
                      />
                    ) : (
                      <ArrowRight aria-hidden="true" />
                    )}
                  </Button>
                </CardContent>
              </Card>
            ),
          )}
        </fieldset>

        <p className="text-xs leading-5 text-slate-500">
          The destination after sign-in is selected from the trusted membership
          returned by <code>/auth/me</code>, never from a browser role setting.
        </p>
      </div>
    </AuthLayout>
  )
}

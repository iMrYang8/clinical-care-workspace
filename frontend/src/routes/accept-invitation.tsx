import { useMutation } from "@tanstack/react-query"
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router"
import { LoaderCircle, ShieldCheck } from "lucide-react"
import { type FormEvent, useEffect, useState } from "react"

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
  const [email, setEmail] = useState("")
  const [token, setToken] = useState("")
  const [fullName, setFullName] = useState("")
  const [password, setPassword] = useState("")

  useEffect(() => {
    const fragmentCode = invitationCodeFromFragment()
    if (fragmentCode) setToken(fragmentCode)
    if (window.location.hash) {
      // Fragments are not sent to the server; remove the one-time code from
      // browser history as soon as it has been copied into component memory.
      window.history.replaceState(null, "", window.location.pathname)
    }
  }, [])

  const acceptance = useMutation({
    mutationFn: authApi.acceptInvitation,
    onSuccess: async () => {
      await navigate({ to: "/login", replace: true })
    },
  })

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    acceptance.mutate({
      email,
      token,
      password,
      full_name: fullName || null,
    })
  }

  return (
    <AuthLayout>
      <div className="space-y-6">
        <div className="space-y-3">
          <span className="grid size-12 place-items-center rounded-2xl bg-teal-700 text-white">
            <ShieldCheck aria-hidden="true" className="size-6" />
          </span>
          <h1 className="font-serif text-4xl font-semibold text-slate-950">
            Accept clinic invitation
          </h1>
          <p className="leading-7 text-slate-600">
            Verify the invited email and enter the one-time code from the
            message. The code is submitted only in the encrypted request body,
            never in a URL query or server access log.
          </p>
        </div>

        {acceptance.isError && (
          <Alert className="border-red-200 bg-red-50 text-red-900" role="alert">
            <AlertTitle>Invitation was not accepted</AlertTitle>
            <AlertDescription>
              {apiErrorMessage(acceptance.error)}
            </AlertDescription>
          </Alert>
        )}

        <form className="space-y-4" onSubmit={submit}>
          <div className="space-y-2">
            <Label htmlFor="invitation-email">Invited email</Label>
            <Input
              autoComplete="email"
              id="invitation-email"
              onChange={(event) => setEmail(event.target.value)}
              required
              type="email"
              value={email}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="invitation-code">One-time code</Label>
            <Input
              autoComplete="off"
              id="invitation-code"
              minLength={64}
              onChange={(event) => setToken(event.target.value.trim())}
              required
              spellCheck={false}
              value={token}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="invitation-name">Display name</Label>
            <Input
              autoComplete="name"
              id="invitation-name"
              onChange={(event) => setFullName(event.target.value)}
              value={fullName}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="invitation-password">New password</Label>
            <Input
              autoComplete="new-password"
              id="invitation-password"
              minLength={16}
              onChange={(event) => setPassword(event.target.value)}
              required
              type="password"
              value={password}
            />
          </div>
          <Button
            className="w-full"
            disabled={acceptance.isPending}
            type="submit"
          >
            {acceptance.isPending && <LoaderCircle className="animate-spin" />}
            Verify and activate membership
          </Button>
        </form>

        <Button asChild className="w-full" variant="ghost">
          <Link to="/login">Back to login</Link>
        </Button>
      </div>
    </AuthLayout>
  )
}

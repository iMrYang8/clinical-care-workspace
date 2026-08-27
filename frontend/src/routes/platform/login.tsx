import { useMutation } from "@tanstack/react-query"
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router"
import { LoaderCircle, ShieldCheck } from "lucide-react"
import { type FormEvent, useState } from "react"

import { AuthLayout } from "@/components/Common/AuthLayout"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { platformApi } from "@/features/platformApi"

export const Route = createFileRoute("/platform/login")({
  component: PlatformLoginPage,
  head: () => ({ meta: [{ title: "Platform administration · Nightingale" }] }),
})

function PlatformLoginPage() {
  const navigate = useNavigate()
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const login = useMutation({
    mutationFn: () => platformApi.login(email, password),
    onSuccess: () => navigate({ to: "/platform" }),
  })
  const submit = (event: FormEvent) => {
    event.preventDefault()
    login.mutate()
  }
  return (
    <AuthLayout>
      <form
        className="space-y-4 rounded-2xl border bg-card p-6 shadow-sm"
        onSubmit={submit}
      >
        <div>
          <p className="text-xs font-bold uppercase tracking-[0.18em] text-primary">
            Platform administration
          </p>
          <h1 className="mt-1 font-serif text-3xl font-semibold">
            Operational oversight across clinics
          </h1>
          <p className="mt-2 text-sm leading-6 text-muted-foreground">
            Every cross-clinic record view is read-only and audited.
          </p>
        </div>
        <Alert>
          <ShieldCheck className="size-4" />
          <AlertDescription>
            Clinical records are read-only in this workspace.
          </AlertDescription>
        </Alert>
        <div className="space-y-2">
          <Label htmlFor="platform-email">Email</Label>
          <Input
            id="platform-email"
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="platform-password">Password</Label>
          <Input
            id="platform-password"
            type="password"
            required
            maxLength={200}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>
        {login.isError && (
          <Alert variant="destructive">
            <AlertDescription>
              The account details could not be verified.
            </AlertDescription>
          </Alert>
        )}
        <Button
          className="w-full min-h-11"
          disabled={login.isPending}
          type="submit"
        >
          {login.isPending && <LoaderCircle className="animate-spin" />} Sign in
        </Button>
        <Button asChild className="w-full" variant="ghost">
          <Link to="/login">Clinic team sign in</Link>
        </Button>
      </form>
    </AuthLayout>
  )
}

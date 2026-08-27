import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { LoaderCircle } from "lucide-react"
import { useEffect } from "react"

import { Button } from "@/components/ui/button"
import useAuth, { roleHome } from "@/hooks/useAuth"

export const Route = createFileRoute("/_layout/")({
  component: RoleHomeRedirect,
})

function RoleHomeRedirect() {
  const navigate = useNavigate()
  const { user, meQuery, logout } = useAuth()

  useEffect(() => {
    if (user) void navigate({ to: roleHome(user.role), replace: true })
  }, [navigate, user])

  return (
    <div className="grid min-h-[50vh] place-items-center text-muted-foreground">
      {meQuery.isError ? (
        <div className="space-y-4 text-center">
          <p>Your clinic account could not be opened.</p>
          <Button onClick={logout}>Return to sign in</Button>
        </div>
      ) : (
        <p className="flex items-center gap-2">
          <LoaderCircle className="animate-spin" /> Opening your workspace…
        </p>
      )}
    </div>
  )
}

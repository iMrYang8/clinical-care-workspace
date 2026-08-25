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
    <div className="grid min-h-[50vh] place-items-center text-slate-500">
      {meQuery.isError ? (
        <div className="space-y-4 text-center">
          <p>Membership could not be resolved.</p>
          <Button onClick={logout}>Clear local session</Button>
        </div>
      ) : (
        <p className="flex items-center gap-2">
          <LoaderCircle className="animate-spin" /> Opening your workspace…
        </p>
      )}
    </div>
  )
}

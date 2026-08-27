import {
  createFileRoute,
  Outlet,
  redirect,
  useNavigate,
} from "@tanstack/react-router"
import { LoaderCircle, LogOut } from "lucide-react"
import { useEffect } from "react"

import { Appearance } from "@/components/Common/Appearance"
import { Brand } from "@/components/Nightingale/Brand"
import { SessionBoundaryError } from "@/components/Nightingale/SessionBoundaryError"
import { Button } from "@/components/ui/button"
import { portalRedirectForRole } from "@/features/portalAccess"
import useAuth, { roleHome, trustedSessionUser } from "@/hooks/useAuth"

export const Route = createFileRoute("/patient/my-care")({
  beforeLoad: async () => {
    const user = await trustedSessionUser()
    if (!user) {
      throw redirect({ to: "/patient/login" })
    }
    const destination = portalRedirectForRole(user.role, "patient")
    if (destination) throw redirect({ to: destination, replace: true })
  },
  component: PatientPortalLayout,
})

function PatientPortalLayout() {
  const navigate = useNavigate()
  const { user, meQuery, logout } = useAuth()

  useEffect(() => {
    if (user && user.role !== "patient") {
      void navigate({ to: roleHome(user.role), replace: true })
    }
  }, [navigate, user])

  if (meQuery.isError) {
    return <SessionBoundaryError error={meQuery.error} onClear={logout} />
  }
  if (meQuery.isLoading || !user || user.role !== "patient") {
    return (
      <div className="grid min-h-svh place-items-center bg-background text-muted-foreground">
        <p className="flex items-center gap-2">
          <LoaderCircle aria-hidden="true" className="animate-spin" />
          Opening My Care…
        </p>
      </div>
    )
  }

  return (
    <div className="min-h-svh bg-background text-foreground">
      <header className="sticky top-0 z-30 border-b border-border/80 bg-background/90 backdrop-blur">
        <div className="mx-auto flex h-16 max-w-6xl items-center gap-3 px-4 sm:px-6">
          <Brand />
          <div className="ml-auto hidden min-w-0 text-right sm:block">
            <p className="truncate text-sm font-medium">
              {user.full_name || user.email}
            </p>
            <p className="text-xs text-muted-foreground">Patient access</p>
          </div>
          <Appearance />
          <Button onClick={logout} size="sm" variant="outline">
            <LogOut aria-hidden="true" />
            <span className="hidden sm:inline">Sign out</span>
          </Button>
        </div>
      </header>
      <main className="mx-auto max-w-6xl p-4 sm:p-6 md:p-8" id="main-content">
        <Outlet />
      </main>
    </div>
  )
}

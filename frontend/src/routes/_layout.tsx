import { createFileRoute, Outlet, redirect } from "@tanstack/react-router"

import AppSidebar from "@/components/Sidebar/AppSidebar"
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar"
import { portalRedirectForRole } from "@/features/portalAccess"
import { trustedSessionUser } from "@/hooks/useAuth"

export const Route = createFileRoute("/_layout")({
  component: Layout,
  beforeLoad: async () => {
    const user = await trustedSessionUser()
    if (!user) {
      throw redirect({
        to: "/login",
      })
    }
    const destination = portalRedirectForRole(user.role, "clinical")
    if (destination) throw redirect({ to: destination, replace: true })
  },
})

function Layout() {
  return (
    <SidebarProvider open>
      <AppSidebar />
      <SidebarInset className="bg-background">
        <header className="sticky top-0 z-30 flex h-16 shrink-0 items-center gap-3 border-b border-border/80 bg-background/90 px-4 backdrop-blur">
          <SidebarTrigger
            className="-ml-1 text-muted-foreground md:hidden"
            aria-label="Open navigation"
          />
          <div className="h-5 w-px bg-border md:hidden" />
          <p className="text-sm text-muted-foreground">
            Clinical care workspace
          </p>
        </header>
        <main className="flex-1 p-4 sm:p-6 md:p-8" id="main-content">
          <div className="mx-auto max-w-7xl">
            <Outlet />
          </div>
        </main>
      </SidebarInset>
    </SidebarProvider>
  )
}

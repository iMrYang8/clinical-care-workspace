import { createFileRoute, Outlet, redirect } from "@tanstack/react-router"

import AppSidebar from "@/components/Sidebar/AppSidebar"
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar"
import { isLoggedIn } from "@/hooks/useAuth"

export const Route = createFileRoute("/_layout")({
  component: Layout,
  beforeLoad: async () => {
    if (!(await isLoggedIn())) {
      throw redirect({
        to: "/login",
      })
    }
  },
})

function Layout() {
  return (
    <SidebarProvider>
      <AppSidebar />
      <SidebarInset className="bg-[#f7faf9]">
        <header className="sticky top-0 z-30 flex h-16 shrink-0 items-center gap-3 border-b border-slate-200/80 bg-white/90 px-4 backdrop-blur">
          <SidebarTrigger className="-ml-1 text-muted-foreground" />
          <div className="h-5 w-px bg-slate-200" />
          <p className="text-sm text-slate-600">Synthetic clinic workspace</p>
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

import { ClipboardList, ShieldCheck } from "lucide-react"

import { SidebarAppearance } from "@/components/Common/Appearance"
import { Brand } from "@/components/Nightingale/Brand"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
} from "@/components/ui/sidebar"
import useAuth from "@/hooks/useAuth"
import { type Item, Main } from "./Main"
import { User } from "./User"

export function AppSidebar() {
  const { user: currentUser } = useAuth()

  const items: Item[] = currentUser
    ? currentUser.role === "patient"
      ? [{ icon: ClipboardList, title: "My care", path: "/my-care" }]
      : currentUser.role === "staff" || currentUser.role === "clinician"
        ? [{ icon: ClipboardList, title: "Care notes", path: "/patients" }]
        : currentUser.role === "admin"
          ? [
              {
                icon: ClipboardList,
                title: "Care notes · read-only",
                path: "/patients",
              },
              { icon: ShieldCheck, title: "Administration", path: "/admin" },
            ]
          : []
    : []

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader className="px-4 py-5 group-data-[collapsible=icon]:items-center group-data-[collapsible=icon]:px-0">
        <Brand />
        <span className="mx-1 mt-3 w-fit rounded-full bg-amber-100 px-2 py-1 text-[0.65rem] font-bold uppercase tracking-[0.14em] text-amber-800 group-data-[collapsible=icon]:hidden">
          Synthetic data
        </span>
      </SidebarHeader>
      <SidebarContent>
        <Main items={items} />
      </SidebarContent>
      <SidebarFooter>
        <SidebarAppearance />
        <User user={currentUser} />
      </SidebarFooter>
    </Sidebar>
  )
}

export default AppSidebar

import { useRouterState } from "@tanstack/react-router"
import {
  AlertTriangle,
  ClipboardList,
  Clock3,
  FileSearch,
  ListChecks,
  MessageCircle,
  ShieldCheck,
  UserRound,
  UserRoundPlus,
} from "lucide-react"

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
  const currentPath = useRouterState({
    select: (state) => state.location.pathname,
  })
  const isPatientRecord =
    currentPath.startsWith("/patients/") && currentPath !== "/patients/new"

  const workspaceItems: Item[] = currentUser
    ? currentUser.role === "staff" || currentUser.role === "clinician"
      ? [
          { icon: ClipboardList, title: "Patients", path: "/patients" },
          {
            icon: UserRoundPlus,
            title: "Add patient",
            path: "/patients/new",
          },
        ]
      : currentUser.role === "admin"
        ? [
            { icon: ClipboardList, title: "Patients", path: "/patients" },
            { icon: ShieldCheck, title: "Administration", path: "/admin" },
          ]
        : []
    : []

  const patientItems: Item[] = isPatientRecord
    ? [
        {
          icon: UserRound,
          title: "Patient overview",
          href: "#patient-overview",
        },
        {
          icon: AlertTriangle,
          title: "Clinical review",
          href: "#clinical-conflicts",
        },
        {
          icon: ListChecks,
          title: "Current priorities",
          href: "#current-priorities",
        },
        { icon: Clock3, title: "Timeline", href: "#timeline" },
        {
          icon: FileSearch,
          title: "Source-linked facts",
          href: "#structured-context",
        },
        {
          icon: MessageCircle,
          title: "Team discussion",
          href: "#team-discussion",
        },
      ]
    : []

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader className="px-4 py-5 group-data-[collapsible=icon]:items-center group-data-[collapsible=icon]:px-0">
        <Brand />
      </SidebarHeader>
      <SidebarContent>
        <Main items={workspaceItems} label="Workspace" />
        {patientItems.length > 0 && (
          <Main items={patientItems} label="Current patient" />
        )}
      </SidebarContent>
      <SidebarFooter>
        <SidebarAppearance />
        <User user={currentUser} />
      </SidebarFooter>
    </Sidebar>
  )
}

export default AppSidebar

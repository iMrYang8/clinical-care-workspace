import { ChevronsUpDown, LogOut } from "lucide-react"
import type { MePublic } from "@/client"
import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from "@/components/ui/sidebar"
import useAuth from "@/hooks/useAuth"
import { getInitials } from "@/utils"

interface UserInfoProps {
  fullName?: string | null
  email?: string | null
}

const roleLabels: Record<MePublic["role"], string> = {
  patient: "Patient",
  staff: "Care staff",
  clinician: "Clinician",
  admin: "Clinic administrator",
  worker: "Care service",
}

function UserInfo({ fullName, email }: UserInfoProps) {
  const primaryLabel = fullName || "Portal user"
  const secondaryLabel = email || "Portal ID and one-time code"
  return (
    <div className="flex items-center gap-2.5 w-full min-w-0">
      <Avatar className="size-8">
        <AvatarFallback className="bg-primary text-primary-foreground">
          {getInitials(primaryLabel)}
        </AvatarFallback>
      </Avatar>
      <div className="flex flex-col items-start min-w-0">
        <p className="text-sm font-medium truncate w-full">{primaryLabel}</p>
        <p className="text-xs text-muted-foreground truncate w-full">
          {secondaryLabel}
        </p>
      </div>
    </div>
  )
}

export function User({ user }: { user?: MePublic }) {
  const { logout } = useAuth()
  const { isMobile, setOpenMobile } = useSidebar()

  if (!user) return null
  const clinicName = user.clinic_name

  const handleLogout = async () => {
    if (isMobile) setOpenMobile(false)
    await logout()
  }

  return (
    <SidebarMenu>
      <SidebarMenuItem>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <SidebarMenuButton
              size="lg"
              className="data-[state=open]:bg-sidebar-accent data-[state=open]:text-sidebar-accent-foreground"
              data-testid="user-menu"
            >
              <UserInfo fullName={user?.full_name} email={user?.email} />
              <ChevronsUpDown className="ml-auto size-4 text-muted-foreground" />
            </SidebarMenuButton>
          </DropdownMenuTrigger>
          <DropdownMenuContent
            className="w-(--radix-dropdown-menu-trigger-width) min-w-56 rounded-lg"
            side={isMobile ? "bottom" : "right"}
            align="end"
            sideOffset={4}
          >
            <DropdownMenuLabel className="p-0 font-normal">
              <UserInfo fullName={user?.full_name} email={user?.email} />
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuLabel className="text-xs font-normal text-muted-foreground">
              {roleLabels[user.role]}
              {clinicName ? ` · ${clinicName}` : ""}
            </DropdownMenuLabel>
            <DropdownMenuItem onClick={handleLogout}>
              <LogOut />
              Sign out
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </SidebarMenuItem>
    </SidebarMenu>
  )
}

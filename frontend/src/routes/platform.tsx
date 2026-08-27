import { createFileRoute, Outlet } from "@tanstack/react-router"

export const Route = createFileRoute("/platform")({ component: PlatformRoot })

function PlatformRoot() {
  return <Outlet />
}

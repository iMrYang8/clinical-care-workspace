import { createFileRoute, Outlet } from "@tanstack/react-router"

export const Route = createFileRoute("/patient")({
  component: PatientPortalRoot,
})

function PatientPortalRoot() {
  return <Outlet />
}

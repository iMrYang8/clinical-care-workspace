import { createFileRoute, redirect } from "@tanstack/react-router"

export const Route = createFileRoute("/my-care")({
  beforeLoad: () => {
    throw redirect({ to: "/patient/my-care", replace: true })
  },
})

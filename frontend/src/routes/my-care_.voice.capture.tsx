import { createFileRoute, redirect } from "@tanstack/react-router"

export const Route = createFileRoute("/my-care_/voice/capture")({
  beforeLoad: () => {
    throw redirect({ to: "/patient/my-care/voice/capture", replace: true })
  },
})

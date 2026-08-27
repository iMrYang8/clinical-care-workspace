import { createFileRoute, redirect } from "@tanstack/react-router"
import { resolveRecordingRouteReference } from "@/features/routeReferences"

export const Route = createFileRoute("/my-care_/voice/$sessionId")({
  beforeLoad: ({ params }) => {
    const recording = resolveRecordingRouteReference(params.sessionId)
    throw redirect({
      to: "/patient/my-care/voice/$sessionId",
      params: { sessionId: recording?.reference ?? params.sessionId },
      replace: true,
    })
  },
})

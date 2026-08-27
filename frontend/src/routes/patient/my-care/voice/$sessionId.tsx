import { createFileRoute, notFound, redirect } from "@tanstack/react-router"

import { PatientVoiceStatus } from "@/components/Voice/PatientVoiceStatus"
import { resolveRecordingRouteReference } from "@/features/routeReferences"

export const Route = createFileRoute("/patient/my-care/voice/$sessionId")({
  beforeLoad: ({ params }) => {
    const recording = resolveRecordingRouteReference(params.sessionId)
    if (!recording) throw notFound()
    if (params.sessionId !== recording.reference) {
      throw redirect({
        to: "/patient/my-care/voice/$sessionId",
        params: { sessionId: recording.reference },
        replace: true,
      })
    }
    return { sessionId: recording.id }
  },
  component: PatientVoiceStatusRoute,
  head: () => ({ meta: [{ title: "Recording status · Nightingale" }] }),
})

function PatientVoiceStatusRoute() {
  const { sessionId } = Route.useRouteContext()
  return <PatientVoiceStatus sessionId={sessionId} />
}

import { useQuery } from "@tanstack/react-query"
import { LoaderCircle, Mic2 } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { voiceSession } from "@/features/voice/voiceApi"

export function PatientVoiceStatus({ sessionId }: { sessionId: string }) {
  const query = useQuery({
    queryKey: ["patient-voice-status", sessionId],
    queryFn: () => voiceSession(sessionId),
    refetchInterval: (current) =>
      current.state.data?.state === "published" ? false : 3_000,
  })
  if (query.isLoading) {
    return (
      <LoaderCircle className="mx-auto mt-20 animate-spin text-amber-700" />
    )
  }
  if (!query.data) return <p>This recording is not available.</p>
  return (
    <Card className="mx-auto max-w-2xl">
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="flex items-center gap-2">
            <Mic2 className="text-amber-700" /> My recording
          </CardTitle>
          <Badge>{query.data.state}</Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm text-slate-600">
          Your care team reviews final results before a patient-facing summary
          is published.
        </p>
        {query.data.patient_summary && (
          <p className="rounded-lg bg-amber-50 p-4 leading-7">
            {query.data.patient_summary}
          </p>
        )}
      </CardContent>
    </Card>
  )
}

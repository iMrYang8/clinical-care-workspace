import { LoaderCircle, Plus, X } from "lucide-react"
import { useState } from "react"

import type { MePublic } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { apiErrorMessage, clinicalApi } from "@/features/api"

type EntryComposerProps = {
  patientId: string
  currentUser: MePublic
  onCreated: () => void | Promise<void>
}

export function EntryComposer({
  patientId,
  currentUser,
  onCreated,
}: EntryComposerProps) {
  const [open, setOpen] = useState(false)
  const [title, setTitle] = useState("")
  const [content, setContent] = useState("")
  const [patientFacing, setPatientFacing] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (!open) {
    return (
      <Button className="min-h-11" onClick={() => setOpen(true)}>
        <Plus /> Add {currentUser.role} entry
      </Button>
    )
  }

  const submit = async () => {
    if (currentUser.role !== "staff" && currentUser.role !== "clinician") return
    setPending(true)
    setError(null)
    try {
      await clinicalApi.createEntry({
        patient_id: patientId,
        section: currentUser.role,
        title: title.trim(),
        content: content.trim(),
        patient_facing: patientFacing,
        origin: "human",
      })
      setTitle("")
      setContent("")
      setPatientFacing(false)
      setOpen(false)
      await onCreated()
    } catch (caught) {
      setError(apiErrorMessage(caught))
    } finally {
      setPending(false)
    }
  }

  return (
    <Card className="border-teal-200 bg-teal-50/40">
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle className="font-serif text-xl">
          New {currentUser.role} entry
        </CardTitle>
        <Button
          aria-label="Close composer"
          onClick={() => setOpen(false)}
          size="icon"
          variant="ghost"
        >
          <X />
        </Button>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-2">
          <Label htmlFor="new-entry-title">Title</Label>
          <Input
            id="new-entry-title"
            onChange={(event) => setTitle(event.target.value)}
            placeholder="e.g. Home visit observation"
            value={title}
          />
        </div>
        <div className="grid gap-2">
          <Label htmlFor="new-entry-content">Care note</Label>
          <textarea
            className="min-h-36 rounded-xl border bg-white p-3 text-sm leading-6 outline-none focus:ring-2 focus:ring-teal-600"
            id="new-entry-content"
            onChange={(event) => setContent(event.target.value)}
            placeholder="Write only observed or reported facts…"
            value={content}
          />
        </div>
        <label className="flex min-h-11 items-center gap-3 rounded-xl border bg-white px-3 py-2 text-sm">
          <input
            checked={patientFacing}
            className="size-4 accent-teal-700"
            onChange={(event) => setPatientFacing(event.target.checked)}
            type="checkbox"
          />
          Publish to patient-facing timeline
        </label>
        {error && (
          <Alert className="border-red-200 bg-red-50 text-red-900">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <Button
          disabled={pending || !title.trim() || !content.trim()}
          onClick={submit}
        >
          {pending ? <LoaderCircle className="animate-spin" /> : <Plus />}
          Create immutable version 1
        </Button>
      </CardContent>
    </Card>
  )
}

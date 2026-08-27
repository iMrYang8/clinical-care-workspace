import { LoaderCircle, Plus } from "lucide-react"
import { useState } from "react"

import type { MePublic } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
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
    <Dialog open={open} onOpenChange={(next) => !pending && setOpen(next)}>
      <Button className="min-h-11" onClick={() => setOpen(true)}>
        <Plus /> Add care note
      </Button>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle className="font-serif text-2xl">
            New care note
          </DialogTitle>
          <DialogDescription>
            Add an observation, decision, or follow-up to this patient record.
          </DialogDescription>
        </DialogHeader>
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault()
            void submit()
          }}
        >
          <div className="grid gap-2">
            <Label htmlFor="new-entry-title">Title</Label>
            <Input
              autoFocus
              id="new-entry-title"
              onChange={(event) => setTitle(event.target.value)}
              placeholder="e.g. Home visit observation"
              value={title}
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="new-entry-content">Care note</Label>
            <textarea
              className="min-h-44 rounded-xl border bg-background p-3 text-sm leading-6 text-foreground outline-none focus:ring-2 focus:ring-primary"
              id="new-entry-content"
              onChange={(event) => setContent(event.target.value)}
              placeholder="Write only observed or reported facts…"
              value={content}
            />
          </div>
          <label className="flex min-h-11 items-center gap-3 rounded-xl border bg-background px-3 py-2 text-sm">
            <input
              checked={patientFacing}
              className="size-4 accent-primary"
              onChange={(event) => setPatientFacing(event.target.checked)}
              type="checkbox"
            />
            {currentUser.role === "staff"
              ? "Request patient sharing"
              : "Approve for patient sharing"}
          </label>
          {error && (
            <Alert className="border-critical/40 bg-critical-muted text-critical-muted-foreground">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          <DialogFooter>
            <Button
              disabled={pending}
              onClick={() => setOpen(false)}
              type="button"
              variant="outline"
            >
              Cancel
            </Button>
            <Button
              disabled={pending || !title.trim() || !content.trim()}
              type="submit"
            >
              {pending ? <LoaderCircle className="animate-spin" /> : <Plus />}
              Create note
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

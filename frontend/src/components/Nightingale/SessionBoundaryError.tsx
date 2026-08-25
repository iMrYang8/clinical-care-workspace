import { ShieldAlert } from "lucide-react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { apiErrorMessage } from "@/features/api"

type SessionBoundaryErrorProps = {
  error: unknown
  onClear: () => void | Promise<void>
}

export function SessionBoundaryError({
  error,
  onClear,
}: SessionBoundaryErrorProps) {
  return (
    <div className="mx-auto grid min-h-[50vh] max-w-xl place-items-center">
      <Alert className="border-amber-200 bg-amber-50 text-amber-950">
        <ShieldAlert />
        <AlertTitle>Membership could not be resolved</AlertTitle>
        <AlertDescription className="space-y-4">
          <p>{apiErrorMessage(error)}</p>
          <p>
            No role or clinic has been inferred in the browser. Clear this local
            session, then choose a server-defined demo persona again.
          </p>
          <Button className="min-h-11" onClick={() => void onClear()}>
            Clear local session
          </Button>
        </AlertDescription>
      </Alert>
    </div>
  )
}

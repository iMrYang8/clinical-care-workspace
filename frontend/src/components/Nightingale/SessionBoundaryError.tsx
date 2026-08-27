import { ShieldAlert } from "lucide-react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { apiErrorMessage } from "@/features/api"

type SessionBoundaryErrorProps = {
  error: unknown
  onClear: () => Promise<boolean>
}

export function SessionBoundaryError({
  error,
  onClear,
}: SessionBoundaryErrorProps) {
  return (
    <div className="mx-auto grid min-h-[50vh] max-w-xl place-items-center">
      <Alert className="border-warning/40 bg-warning/10 text-foreground">
        <ShieldAlert />
        <AlertTitle>Membership could not be resolved</AlertTitle>
        <AlertDescription className="space-y-4">
          <p>{apiErrorMessage(error)}</p>
          <p>
            Your clinic membership could not be verified. Clear this local
            session, then sign in again with your clinic account.
          </p>
          <Button className="min-h-11" onClick={() => void onClear()}>
            Return to sign in
          </Button>
        </AlertDescription>
      </Alert>
    </div>
  )
}

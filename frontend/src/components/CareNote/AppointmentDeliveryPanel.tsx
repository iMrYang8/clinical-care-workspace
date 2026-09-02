import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  CheckCircle2,
  CircleX,
  Clock3,
  LoaderCircle,
  RefreshCw,
  Send,
} from "lucide-react"
import { useState } from "react"

import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  apiErrorMessage,
  clinicalApi,
  type NotificationDelivery,
} from "@/features/api"
import { formatSingaporeDateTime } from "@/lib/dateTime"

const terminalStates = new Set([
  "delivered",
  "failed",
  "acknowledged",
  "revoked",
])

function DeliveryBadge({ delivery }: { delivery: NotificationDelivery }) {
  if (delivery.state === "acknowledged")
    return (
      <Badge className="bg-success-muted text-success-muted-foreground">
        <CheckCircle2 /> Acknowledged
      </Badge>
    )
  if (delivery.state === "delivered")
    return (
      <Badge className="bg-success-muted text-success-muted-foreground">
        Delivered
      </Badge>
    )
  if (delivery.state === "failed")
    return <Badge variant="destructive">Failed</Badge>
  if (delivery.state === "revoked")
    return <Badge variant="outline">Revoked</Badge>
  return (
    <Badge className="bg-warning-muted text-warning-muted-foreground">
      <Clock3 /> {delivery.state === "submitted" ? "Submitted" : "Queued"}
    </Badge>
  )
}

export function AppointmentDeliveryPanel({
  patientId,
  visitId,
}: {
  patientId: string
  visitId: string
}) {
  const queryClient = useQueryClient()
  const [channel, setChannel] = useState<"email" | "sms" | "whatsapp">("sms")
  const [destination, setDestination] = useState("")
  const queryKey = ["patients", patientId, "visits", visitId, "notifications"]
  const deliveries = useQuery({
    queryKey,
    queryFn: () => clinicalApi.notificationDeliveries(patientId, visitId),
    refetchInterval: (query) =>
      (query.state.data ?? []).some((item) => !terminalStates.has(item.state))
        ? 5_000
        : false,
  })
  const refresh = () => queryClient.invalidateQueries({ queryKey })
  const create = useMutation({
    mutationFn: () =>
      clinicalApi.createAppointmentNotification(patientId, visitId, {
        channel,
        destination: destination.trim(),
      }),
    onSuccess: async () => {
      setDestination("")
      await refresh()
    },
  })
  const resend = useMutation({
    mutationFn: (notificationId: string) =>
      clinicalApi.resendNotification(notificationId),
    onSuccess: refresh,
  })
  const revoke = useMutation({
    mutationFn: (notificationId: string) =>
      clinicalApi.revokeNotification(notificationId),
    onSuccess: refresh,
  })
  const error = deliveries.error ?? create.error ?? resend.error ?? revoke.error

  return (
    <Card className="order-4" data-testid="appointment-delivery-panel">
      <CardHeader className="pb-3">
        <CardTitle className="font-serif text-xl">
          Appointment delivery
        </CardTitle>
        <p className="text-sm leading-6 text-muted-foreground">
          Delivery receipts are tracked separately from creation. A queued
          message is not treated as delivered.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        <form
          className="space-y-3 rounded-xl border bg-muted/30 p-3"
          onSubmit={(event) => {
            event.preventDefault()
            create.mutate()
          }}
        >
          <div className="grid gap-3 sm:grid-cols-[9rem_1fr]">
            <div className="space-y-2">
              <Label htmlFor="appointment-channel">Channel</Label>
              <select
                className="h-10 w-full rounded-md border bg-background px-3 text-sm"
                id="appointment-channel"
                onChange={(event) =>
                  setChannel(event.target.value as "email" | "sms" | "whatsapp")
                }
                value={channel}
              >
                <option value="sms">SMS</option>
                <option value="whatsapp">WhatsApp</option>
                <option value="email">Email</option>
              </select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="appointment-destination">
                {channel === "email" ? "Email" : "Mobile phone"}
              </Label>
              <Input
                id="appointment-destination"
                inputMode={channel === "email" ? "email" : "tel"}
                onChange={(event) => setDestination(event.target.value)}
                required
                type={channel === "email" ? "email" : "tel"}
                value={destination}
              />
            </div>
          </div>
          <Button
            className="w-full"
            disabled={!destination.trim() || create.isPending}
            size="sm"
            type="submit"
          >
            {create.isPending ? (
              <LoaderCircle className="animate-spin" />
            ) : (
              <Send />
            )}
            Queue appointment message
          </Button>
        </form>

        {(deliveries.data ?? []).map((delivery) => (
          <div className="space-y-2 rounded-xl border p-3" key={delivery.id}>
            <div className="flex items-start justify-between gap-2">
              <div>
                <p className="font-medium capitalize">
                  {delivery.channel} · {delivery.destination_masked}
                </p>
                <p className="mt-1 text-xs text-muted-foreground">
                  Queued {formatSingaporeDateTime(delivery.created_at)}
                </p>
              </div>
              <DeliveryBadge delivery={delivery} />
            </div>
            {delivery.state === "failed" && (
              <p className="text-xs text-critical-muted-foreground">
                Provider delivery failed
                {delivery.failed_at
                  ? ` · ${formatSingaporeDateTime(delivery.failed_at)}`
                  : ""}
              </p>
            )}
            {delivery.state === "queued" && delivery.available_at && (
              <p className="text-xs text-muted-foreground">
                Available for delivery{" "}
                {formatSingaporeDateTime(delivery.available_at)} ·{" "}
                {delivery.attempt_count} attempt
                {delivery.attempt_count === 1 ? "" : "s"}
              </p>
            )}
            {(delivery.receipts?.length ?? 0) > 0 && (
              <p className="text-xs text-muted-foreground">
                {delivery.receipts?.length} signed provider receipt
                {delivery.receipts?.length === 1 ? "" : "s"} recorded
              </p>
            )}
            <div className="flex flex-wrap gap-2">
              {delivery.state === "failed" && (
                <Button
                  disabled={resend.isPending}
                  onClick={() => resend.mutate(delivery.id)}
                  size="sm"
                  variant="outline"
                >
                  <RefreshCw /> Resend
                </Button>
              )}
              {!terminalStates.has(delivery.state) &&
                delivery.state !== "failed" && (
                  <Button
                    disabled={revoke.isPending}
                    onClick={() => revoke.mutate(delivery.id)}
                    size="sm"
                    variant="outline"
                  >
                    <CircleX /> Revoke
                  </Button>
                )}
            </div>
          </div>
        ))}
        {deliveries.isSuccess && deliveries.data.length === 0 && (
          <p className="text-sm text-muted-foreground">
            No appointment messages have been queued for this visit.
          </p>
        )}
        {error && (
          <Alert className="border-critical/40 bg-critical-muted text-critical-muted-foreground">
            <AlertDescription>{apiErrorMessage(error)}</AlertDescription>
          </Alert>
        )}
      </CardContent>
    </Card>
  )
}

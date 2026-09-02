import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { clinicalApi, type NotificationDelivery } from "@/features/api"
import { AppointmentDeliveryPanel } from "./AppointmentDeliveryPanel"

const failedDelivery: NotificationDelivery = {
  id: "notification-1",
  patient_id: "patient-1",
  visit_id: "visit-1",
  publication_id: null,
  purpose: "appointment",
  channel: "sms",
  destination_masked: "***1234",
  state: "failed",
  available_at: "2026-09-01T01:00:00Z",
  submitted_at: "2026-09-01T01:00:01Z",
  delivered_at: null,
  failed_at: "2026-09-01T01:00:02Z",
  acknowledged_at: null,
  revoked_at: null,
  created_at: "2026-09-01T01:00:00Z",
  updated_at: "2026-09-01T01:00:02Z",
  attempt_count: 1,
  receipts: [
    {
      id: "receipt-1",
      notification_id: "notification-1",
      provider: "observable-fake-sms",
      provider_event_id: "event-1",
      provider_message_id: "message-1",
      event_type: "failed",
      occurred_at: "2026-09-01T01:00:02Z",
      received_at: "2026-09-01T01:00:03Z",
      signature_verified: true,
    },
  ],
}

function renderPanel() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={queryClient}>
      <AppointmentDeliveryPanel patientId="patient-1" visitId="visit-1" />
    </QueryClientProvider>,
  )
}

afterEach(() => vi.restoreAllMocks())

describe("appointment delivery lifecycle", () => {
  it("distinguishes a failed provider receipt from delivery and allows explicit resend", async () => {
    vi.spyOn(clinicalApi, "notificationDeliveries").mockResolvedValue([
      failedDelivery,
    ])
    const resend = vi
      .spyOn(clinicalApi, "resendNotification")
      .mockResolvedValue({ ...failedDelivery, state: "submitted" })
    renderPanel()

    expect(await screen.findByText("Failed")).toBeInTheDocument()
    expect(
      screen.getByText(/1 signed provider receipt recorded/),
    ).toBeInTheDocument()
    expect(screen.queryByText("Delivered")).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Resend" }))
    await waitFor(() => expect(resend).toHaveBeenCalledWith("notification-1"))
  })

  it("queues an appointment using the selected deterministic provider channel", async () => {
    vi.spyOn(clinicalApi, "notificationDeliveries").mockResolvedValue([])
    const create = vi
      .spyOn(clinicalApi, "createAppointmentNotification")
      .mockResolvedValue({
        ...failedDelivery,
        id: "notification-2",
        state: "queued",
        attempt_count: 0,
        receipts: [],
      })
    renderPanel()

    fireEvent.change(await screen.findByLabelText("Mobile phone"), {
      target: { value: "+6512345678" },
    })
    fireEvent.click(
      screen.getByRole("button", { name: "Queue appointment message" }),
    )

    await waitFor(() =>
      expect(create).toHaveBeenCalledWith("patient-1", "visit-1", {
        channel: "sms",
        destination: "+6512345678",
      }),
    )
  })
})

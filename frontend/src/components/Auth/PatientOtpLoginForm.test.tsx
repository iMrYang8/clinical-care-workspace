import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import type { ReactNode } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { patientAccessApi } from "@/features/api"
import PatientOtpLoginForm, { normalizePortalId } from "./PatientOtpLoginForm"

const navigate = vi.hoisted(() => vi.fn())

vi.mock("@tanstack/react-router", async () => {
  const React = await import("react")
  return {
    Link: ({ children, to }: { children: ReactNode; to: string }) =>
      React.createElement("a", { href: to }, children),
    useNavigate: () => navigate,
  }
})

function renderForm() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <PatientOtpLoginForm />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  navigate.mockReset()
})

describe("patient portal ID", () => {
  it("normalizes the non-secret identifier without accepting a phone destination", () => {
    expect(normalizePortalId("  mycare-ab12  ")).toBe("MYCARE-AB12")
  })

  it("uses the opaque challenge token for OTP verification and never asks for email", async () => {
    const request = vi
      .spyOn(patientAccessApi, "requestLoginOtp")
      .mockResolvedValue({
        challenge_id: "challenge-id",
        challenge_token: "opaque-challenge-token",
        purpose: "login",
        portal_id: "MYCARE-AB12",
        masked_phone: "+65 **** 1234",
        expires_at: "2026-09-01T01:10:00Z",
        resend_available_at: "2026-09-01T01:01:00Z",
        attempts_remaining: 5,
        notification_id: "notification-1",
        delivery_state: "submitted",
      })
    const verify = vi.spyOn(patientAccessApi, "verifyOtp").mockResolvedValue({
      access: {
        credential_id: "credential-1",
        patient_id: "patient-1",
        clinic_id: "clinic-1",
        portal_id: "MYCARE-AB12",
        masked_phone: "+65 **** 1234",
        access_state: "active",
      },
      token: { access_token: "cookie-backed-token", token_type: "bearer" },
    })
    renderForm()

    expect(screen.queryByLabelText("Email")).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText("Portal ID"), {
      target: { value: "  mycare-ab12 " },
    })
    fireEvent.click(screen.getByRole("button", { name: "Send one-time code" }))
    await waitFor(() =>
      expect(request).toHaveBeenCalledWith({ portal_id: "MYCARE-AB12" }),
    )
    expect(
      await screen.findByText(/Enter the code sent to \+65 \*\*\*\* 1234/),
    ).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText("One-time code"), {
      target: { value: "123456" },
    })
    fireEvent.click(screen.getByRole("button", { name: "Verify and sign in" }))
    await waitFor(() =>
      expect(verify).toHaveBeenCalledWith({
        challenge_token: "opaque-challenge-token",
        otp: "123456",
      }),
    )
    expect(navigate).toHaveBeenCalledWith({
      replace: true,
      to: "/patient/my-care",
    })
  })
})

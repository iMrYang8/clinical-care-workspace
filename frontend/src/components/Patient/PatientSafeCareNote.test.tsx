import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import type { PatientSafeApi } from "./PatientSafeCareNote"
import { PatientSafeCareNote } from "./PatientSafeCareNote"

describe("PatientSafeCareNote request boundary", () => {
  it("loads only patient-safe resources and never requests comments", async () => {
    const requests: string[] = []
    const api: PatientSafeApi = {
      patients: vi.fn(async () => {
        requests.push("patients")
        return [{ id: "patient-1", display_name: "Alex Synthetic" }]
      }),
      timeline: vi.fn(async () => {
        requests.push("timeline")
        return []
      }),
      glance: vi.fn(async () => {
        requests.push("glance")
        return {
          patient_id: "patient-1",
          source: "precomputed" as const,
          generated_at: "2026-08-25T00:00:00Z",
          cards: [],
        }
      }),
      resolveProvenance: vi.fn(async () => {
        requests.push("provenance")
        throw new Error("not used")
      }),
      createInsight: vi.fn(async () => {
        requests.push("create-insight")
        throw new Error("not used")
      }),
    }
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })

    render(
      <QueryClientProvider client={queryClient}>
        <PatientSafeCareNote api={api} />
      </QueryClientProvider>,
    )

    expect(
      await screen.findByText(/My Care · Alex Synthetic/),
    ).toBeInTheDocument()
    expect(
      await screen.findByText("No published entries yet"),
    ).toBeInTheDocument()
    expect(requests.sort()).toEqual(["glance", "patients", "timeline"])
    expect(requests).not.toContain("comments")
  })

  it("shows a patient-safe message when sharing receipts do not load", async () => {
    const api: PatientSafeApi = {
      patients: vi.fn(async () => [
        { id: "patient-1", display_name: "Alex Synthetic" },
      ]),
      timeline: vi.fn(async () => []),
      glance: vi.fn(async () => ({
        patient_id: "patient-1",
        source: "precomputed" as const,
        generated_at: "2026-08-25T00:00:00Z",
        cards: [],
      })),
      publicationReceipts: vi.fn(async () => {
        throw new Error("SQLSTATE 42P01 PRIVATE_RECEIPT_STORE_CODE")
      }),
      resolveProvenance: vi.fn(async () => {
        throw new Error("not used")
      }),
      createInsight: vi.fn(async () => {
        throw new Error("not used")
      }),
    }
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })

    render(
      <QueryClientProvider client={queryClient}>
        <PatientSafeCareNote api={api} />
      </QueryClientProvider>,
    )

    expect(
      await screen.findByText("Sharing history unavailable"),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/Approval and withdrawal history did not load/),
    ).toBeInTheDocument()
    expect(
      screen.queryByText(/SQLSTATE|PRIVATE_RECEIPT_STORE_CODE/),
    ).not.toBeInTheDocument()
  })
})

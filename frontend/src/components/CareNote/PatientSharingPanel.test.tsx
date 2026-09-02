import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { MePublic } from "@/client"
import {
  type ClinicalTimelineEntry,
  clinicalApi,
  type PatientPublication,
  type PatientSharingRequest,
} from "@/features/api"
import { PatientSharingPanel } from "./PatientSharingPanel"

const entry = {
  id: "11111111-1111-4111-8111-111111111111",
  patient_id: "22222222-2222-4222-8222-222222222222",
  section: "staff",
  origin: "human",
  author_id: "33333333-3333-4333-8333-333333333333",
  entry_type: "manual_staff_note",
  patient_facing: false,
  version_id: "44444444-4444-4444-8444-444444444444",
  version_no: 1,
  title: "Home hydration update",
  content: "Patient is following the reviewed fluid plan.",
  created_at: "2026-08-28T01:00:00Z",
  occurred_at: "2026-08-28T01:00:00Z",
} as ClinicalTimelineEntry

const request: PatientSharingRequest = {
  id: "55555555-5555-4555-8555-555555555555",
  patient_id: entry.patient_id,
  entry_id: entry.id,
  entry_version_id: entry.version_id,
  entry_title: entry.title,
  entry_section: "staff",
  entry_origin: "human",
  requested_by_name: "Care Staff",
  status: "pending",
  created_at: "2026-08-28T01:00:00Z",
  reviewed_at: null,
  reviewed_by_name: null,
  publication_id: null,
}

function renderPanel(
  role: "staff" | "clinician",
  clinicalFacts: React.ComponentProps<
    typeof PatientSharingPanel
  >["clinicalFacts"] = [],
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const currentUser = {
    role,
    clinic_id: "66666666-6666-4666-8666-666666666666",
  } as MePublic
  render(
    <QueryClientProvider client={queryClient}>
      <PatientSharingPanel
        clinicalFacts={clinicalFacts}
        clinicalFactsReady
        currentUser={currentUser}
        onChanged={vi.fn()}
        patientId={entry.patient_id}
        timeline={[entry]}
      />
    </QueryClientProvider>,
  )
}

afterEach(() => vi.restoreAllMocks())

describe("patient sharing workbench", () => {
  it("lets care staff submit the current immutable note for review", async () => {
    vi.spyOn(clinicalApi, "patientSharingRequests").mockResolvedValue([])
    vi.spyOn(clinicalApi, "patientPublications").mockResolvedValue([])
    const submit = vi
      .spyOn(clinicalApi, "requestPatientSharing")
      .mockResolvedValue(request)
    renderPanel("staff")

    fireEvent.change(await screen.findByLabelText("Care staff note"), {
      target: { value: entry.id },
    })
    fireEvent.click(screen.getByTestId("request-patient-sharing"))

    await waitFor(() =>
      expect(submit).toHaveBeenCalledWith(entry.id, entry.version_id),
    )
  })

  it("shows the exact requested version before clinician publication", async () => {
    vi.spyOn(clinicalApi, "patientSharingRequests").mockResolvedValue([request])
    vi.spyOn(clinicalApi, "patientPublications").mockResolvedValue([])
    vi.spyOn(clinicalApi, "versions").mockResolvedValue([
      {
        id: entry.version_id,
        entry_id: entry.id,
        version_no: 1,
        title: entry.title,
        content: entry.content,
        content_sha256: "a".repeat(64),
        author_id: entry.author_id!,
        reverted_from_version_id: null,
        created_at: entry.created_at,
      },
    ])
    const approve = vi
      .spyOn(clinicalApi, "approvePatientSharing")
      .mockResolvedValue({
        id: "77777777-7777-4777-8777-777777777777",
        patient_id: entry.patient_id,
        entry_id: entry.id,
        entry_version_id: entry.version_id,
        entry_title: entry.title,
        approved_by_name: "Clinician",
        approval_policy_version: "patient-sharing-v1",
        approved_at: "2026-08-28T02:00:00Z",
        withdrawn_at: null,
        items: [
          {
            support_state: "human_asserted",
            confidence_band: "not_applicable",
          },
        ],
      })
    renderPanel("clinician")

    fireEvent.click(
      await screen.findByRole("button", { name: "Review exact version" }),
    )
    expect(await screen.findByText(entry.content)).toBeInTheDocument()
    fireEvent.click(screen.getByTestId("approve-patient-sharing"))

    await waitFor(() => expect(approve).toHaveBeenCalledWith(request.id, []))
  })

  it("requires confirmation before withdrawing an active publication", async () => {
    vi.spyOn(clinicalApi, "patientSharingRequests").mockResolvedValue([])
    const publication = {
      id: "77777777-7777-4777-8777-777777777777",
      patient_id: entry.patient_id,
      entry_id: entry.id,
      entry_version_id: entry.version_id,
      entry_title: entry.title,
      approved_by_name: "Clinician",
      approval_policy_version: "patient-sharing-v1",
      approved_at: "2026-08-28T02:00:00Z",
      withdrawn_at: null,
      items: [
        {
          support_state: "human_asserted",
          confidence_band: "not_applicable",
        },
      ],
    }
    vi.spyOn(clinicalApi, "patientPublications").mockResolvedValue([
      publication,
    ])
    const withdraw = vi
      .spyOn(clinicalApi, "withdrawPatientPublication")
      .mockResolvedValue({
        ...publication,
        withdrawn_at: "2026-08-28T03:00:00Z",
      })
    renderPanel("clinician")

    fireEvent.click(
      await screen.findByRole("button", {
        name: "Withdraw from patient portal",
      }),
    )
    expect(screen.getByRole("dialog")).toHaveTextContent(entry.title)
    fireEvent.click(screen.getByTestId("withdraw-patient-sharing"))

    await waitFor(() => expect(withdraw).toHaveBeenCalledWith(publication.id))
  })

  it("reuses the correction key after a lost response and surfaces delivery failure", async () => {
    vi.spyOn(clinicalApi, "patientSharingRequests").mockResolvedValue([])
    const publication = {
      id: "77777777-7777-4777-8777-777777777777",
      patient_id: entry.patient_id,
      entry_id: entry.id,
      entry_version_id: "88888888-8888-4888-8888-888888888888",
      supersedes_publication_id: null,
      entry_title: entry.title,
      approved_by_name: "Clinician",
      approval_policy_version: "patient-sharing-v1",
      approved_at: "2026-08-28T02:00:00Z",
      withdrawn_at: null,
      items: [],
      medication_review_complete: false,
      medication_reviews: [],
      correction_reason_code: null,
      replacement_publication_id: null,
      acknowledgement_state: "not_required",
      outreach_required: false,
      notification_id: null,
      notification_state: null,
      delivery_warning: null,
    } satisfies PatientPublication
    vi.spyOn(clinicalApi, "patientPublications").mockResolvedValue([
      publication,
    ])
    const correct = vi
      .spyOn(clinicalApi, "correctPatientPublication")
      .mockRejectedValueOnce(new Error("Network response lost"))
      .mockResolvedValueOnce({
        ...publication,
        id: "99999999-9999-4999-8999-999999999999",
        supersedes_publication_id: publication.id,
        outreach_required: true,
        notification_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        notification_state: "failed",
        delivery_warning: "notification_delivery_failed",
      })
    renderPanel("clinician")

    fireEvent.click(
      await screen.findByRole("button", { name: "Publish linked correction" }),
    )
    fireEvent.change(screen.getByLabelText("Replacement version"), {
      target: { value: entry.version_id },
    })
    const submit = screen.getByRole("button", {
      name: "Withdraw, replace, and notify",
    })
    fireEvent.click(submit)
    await screen.findByText(
      "Patient sharing could not be completed. Review the note and try again.",
    )
    fireEvent.click(submit)

    await waitFor(() => expect(correct).toHaveBeenCalledTimes(2))
    expect(correct.mock.calls[0]?.[2]).toBeTruthy()
    expect(correct.mock.calls[1]?.[2]).toBe(correct.mock.calls[0]?.[2])
    expect(
      await screen.findByText(/Correction published, but delivery failed/),
    ).toBeInTheDocument()
  })

  it("fails closed when exact-version medication regimen data is incomplete", async () => {
    vi.spyOn(clinicalApi, "patientSharingRequests").mockResolvedValue([request])
    vi.spyOn(clinicalApi, "patientPublications").mockResolvedValue([])
    vi.spyOn(clinicalApi, "versions").mockResolvedValue([
      {
        id: entry.version_id,
        entry_id: entry.id,
        version_no: 1,
        title: entry.title,
        content: entry.content,
        content_sha256: "a".repeat(64),
        author_id: entry.author_id!,
        reverted_from_version_id: null,
        created_at: entry.created_at,
      },
    ])
    renderPanel("clinician", [
      {
        id: "assertion-1",
        fact_type: "medication",
        subject: "amoxicillin",
        normalized_value: "amoxicillin",
        clinical_status: "active",
        effective_time: null,
        origin: "human",
        source_entry_version_id: entry.version_id,
        provenance_pointer_id: "pointer-1",
        medication: "amoxicillin",
        dose_value: 500,
        dose_unit: "mg",
        route: "oral",
        frequency: null,
      },
    ])

    fireEvent.click(
      await screen.findByRole("button", { name: "Review exact version" }),
    )
    expect(
      await screen.findByText(/Structured medication review data.*incomplete/),
    ).toBeInTheDocument()
    expect(screen.getByTestId("approve-patient-sharing")).toBeDisabled()
  })

  it("shows every sharing request instead of silently truncating the queue", async () => {
    const requests = Array.from({ length: 6 }, (_, index) => ({
      ...request,
      id: `55555555-5555-4555-8555-55555555555${index}`,
      entry_title: `Care note ${index + 1}`,
    }))
    vi.spyOn(clinicalApi, "patientSharingRequests").mockResolvedValue(requests)
    vi.spyOn(clinicalApi, "patientPublications").mockResolvedValue([])
    renderPanel("clinician")

    expect(await screen.findByText("Care note 6")).toBeInTheDocument()
    expect(screen.getAllByText("Awaiting review")).toHaveLength(6)
  })

  it("does not present a superseded version as awaiting review", async () => {
    vi.spyOn(clinicalApi, "patientSharingRequests").mockResolvedValue([
      { ...request, status: "superseded" },
    ])
    vi.spyOn(clinicalApi, "patientPublications").mockResolvedValue([])
    renderPanel("clinician")

    expect(
      await screen.findByText("Newer version submitted"),
    ).toBeInTheDocument()
    expect(screen.queryByText("Awaiting review")).not.toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: "Review exact version" }),
    ).not.toBeInTheDocument()
  })

  it("does not present a failed request query as an empty review queue", async () => {
    vi.spyOn(clinicalApi, "patientSharingRequests").mockRejectedValue(
      new Error("SQLSTATE 42P01 INTERNAL_REQUEST_QUEUE"),
    )
    vi.spyOn(clinicalApi, "patientPublications").mockResolvedValue([])
    renderPanel("staff")

    expect(
      await screen.findByText(/Patient sharing requests could not be loaded/),
    ).toBeInTheDocument()
    expect(
      screen.queryByText("No patient sharing requests yet."),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByTestId("request-patient-sharing"),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByText(/SQLSTATE|INTERNAL_REQUEST_QUEUE/),
    ).not.toBeInTheDocument()
  })

  it("surfaces a publication lookup failure before staff can submit", async () => {
    vi.spyOn(clinicalApi, "patientSharingRequests").mockResolvedValue([])
    vi.spyOn(clinicalApi, "patientPublications").mockRejectedValue(
      new Error("PRIVATE_PUBLICATION_STORE_CODE"),
    )
    renderPanel("staff")

    expect(
      await screen.findByText(/Currently shared notes could not be loaded/),
    ).toBeInTheDocument()
    expect(
      screen.queryByTestId("request-patient-sharing"),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByText(/PRIVATE_PUBLICATION_STORE_CODE/),
    ).not.toBeInTheDocument()
  })
})

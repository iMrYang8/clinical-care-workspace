import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { AxiosError } from "axios"
import type { ReactElement } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { clinicalApi } from "@/features/api"
import type { EditorPresenceRecord } from "@/features/editorPresence"
import { EntryEditor } from "./EntryEditor"

const selfPresence: EditorPresenceRecord = {
  clinic_id: "clinic-1",
  patient_id: "patient-1",
  entry_id: "entry-1",
  entry_version_id: "version-1",
  actor_id: "actor-self",
  actor_role: "clinician",
  actor_display_name: "Dr Self",
  expires_at: "2099-01-01T00:00:00Z",
}

function renderEditor(component: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(component, {
    wrapper: ({ children }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    ),
  })
}

function conflictError() {
  const error = new AxiosError("Conflict")
  Object.assign(error, {
    response: {
      status: 409,
      data: {
        detail: {
          code: "VERSION_CONFLICT",
          message: "The entry changed since your draft began",
          current_version_id: "server-version-2",
        },
      },
    },
  })
  return error
}

describe("EntryEditor optimistic concurrency", () => {
  beforeEach(() => {
    vi.spyOn(clinicalApi, "heartbeatEditorPresence").mockResolvedValue(
      selfPresence,
    )
  })

  afterEach(() => vi.restoreAllMocks())

  it("keeps the local draft visible after a deterministic 409", async () => {
    const onSave = vi.fn().mockRejectedValue(conflictError())
    renderEditor(
      <EntryEditor
        actorId="actor-self"
        entryId="entry-1"
        initialDraft={{
          title: "Initial title",
          content: "Original care note",
          patient_facing: false,
        }}
        onCancel={vi.fn()}
        onSave={onSave}
        versionId="version-1"
      />,
    )

    fireEvent.change(screen.getByLabelText("Entry title"), {
      target: { value: "My unsaved clinical draft" },
    })
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }))

    await waitFor(() =>
      expect(screen.getByRole("dialog")).toHaveTextContent("Version conflict"),
    )
    expect(screen.getByRole("dialog")).toHaveTextContent(
      "My unsaved clinical draft",
    )
    expect(screen.getByLabelText("Entry title")).toHaveValue(
      "My unsaved clinical draft",
    )
    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({ title: "My unsaved clinical draft" }),
      "version-1",
    )
  })

  it("freezes the base ETag when a refreshed version arrives", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined)
    const props = {
      initialDraft: {
        title: "Initial title",
        content: "Original care note",
        patient_facing: false,
      },
      onCancel: vi.fn(),
      onSave,
    }
    const view = renderEditor(
      <EntryEditor
        {...props}
        actorId="actor-self"
        currentVersionId="version-1"
        entryId="entry-1"
        versionId="version-1"
      />,
    )

    fireEvent.change(screen.getByLabelText("Entry title"), {
      target: { value: "My draft based on version one" },
    })
    view.rerender(
      <EntryEditor
        {...props}
        actorId="actor-self"
        currentVersionId="version-2"
        entryId="entry-1"
        initialDraft={{
          ...props.initialDraft,
          title: "Another session's title",
        }}
        versionId="version-2"
      />,
    )

    expect(await screen.findByRole("dialog")).toHaveTextContent(
      "changed while your draft was open",
    )
    expect(screen.getByRole("dialog")).toHaveTextContent(
      "My draft based on version one",
    )
    fireEvent.click(screen.getByRole("button", { name: "Keep editing" }))
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }))

    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith(
        expect.objectContaining({ title: "My draft based on version one" }),
        "version-1",
      ),
    )
  })

  it("loads latest, requires an explicit reconciliation, then saves against the latest ETag", async () => {
    const onSave = vi
      .fn()
      .mockRejectedValueOnce(conflictError())
      .mockResolvedValue(undefined)
    const onLoadLatest = vi.fn().mockResolvedValue({
      draft: {
        title: "Latest server title",
        content: "Latest server wording",
        patient_facing: false,
      },
      versionId: "server-version-2",
    })
    renderEditor(
      <EntryEditor
        actorId="actor-self"
        entryId="entry-1"
        initialDraft={{
          title: "Initial title",
          content: "Original care note",
          patient_facing: true,
        }}
        onCancel={vi.fn()}
        onLoadLatest={onLoadLatest}
        onSave={onSave}
        versionId="version-1"
      />,
    )
    fireEvent.change(screen.getByLabelText("Entry title"), {
      target: { value: "My local title" },
    })
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }))
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Load latest and reconcile my draft",
      }),
    )
    expect(await screen.findByText("Latest saved note")).toBeInTheDocument()
    expect(screen.getByText("Latest server wording")).toBeInTheDocument()
    expect(screen.getByText("My local draft")).toBeInTheDocument()
    expect(
      screen.getByRole("checkbox", {
        name: "Request patient sharing in the reconciled result",
      }),
    ).toBeChecked()
    fireEvent.change(screen.getByLabelText("Reconciled care note"), {
      target: { value: "Clinician reconciled wording" },
    })
    fireEvent.click(
      screen.getByRole("button", { name: "Continue with reconciled draft" }),
    )
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    )
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }))
    await waitFor(() =>
      expect(onSave).toHaveBeenLastCalledWith(
        expect.objectContaining({
          content: "Clinician reconciled wording",
          patient_facing: true,
        }),
        "server-version-2",
      ),
    )
  })

  it("heartbeats without draft content and displays only other active editors on the base version", async () => {
    const onPresence = vi.fn()
    const heartbeat = vi.mocked(clinicalApi.heartbeatEditorPresence)
    const activePeer: EditorPresenceRecord = {
      ...selfPresence,
      actor_id: "actor-peer",
      actor_display_name: "Dr Lee",
    }
    renderEditor(
      <EntryEditor
        actorId="actor-self"
        editorPresence={[
          selfPresence,
          activePeer,
          {
            ...activePeer,
            actor_id: "expired-peer",
            actor_display_name: "Expired editor",
            expires_at: "2000-01-01T00:00:00Z",
          },
          {
            ...activePeer,
            actor_id: "other-version-peer",
            actor_display_name: "Different version editor",
            entry_version_id: "version-2",
          },
        ]}
        entryId="entry-1"
        initialDraft={{
          title: "Private draft title",
          content: "Private draft content",
          patient_facing: false,
        }}
        onCancel={vi.fn()}
        onPresence={onPresence}
        onSave={vi.fn()}
        versionId="version-1"
      />,
    )

    expect(screen.getByRole("status")).toHaveTextContent(
      "Dr Lee is also editing this saved version",
    )
    expect(screen.getByRole("status")).not.toHaveTextContent("Dr Self")
    expect(screen.getByRole("status")).not.toHaveTextContent("Expired editor")
    expect(screen.getByRole("status")).not.toHaveTextContent(
      "Different version editor",
    )
    await waitFor(() =>
      expect(heartbeat).toHaveBeenCalledWith(
        "entry-1",
        "version-1",
        expect.any(AbortSignal),
      ),
    )
    expect(JSON.stringify(heartbeat.mock.calls[0])).not.toContain(
      "Private draft",
    )
    await waitFor(() => expect(onPresence).toHaveBeenCalledWith(selfPresence))
  })
})

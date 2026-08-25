import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { AxiosError } from "axios"
import { describe, expect, it, vi } from "vitest"

import { EntryEditor } from "./EntryEditor"

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
  it("keeps the local draft visible after a deterministic 409", async () => {
    const onSave = vi.fn().mockRejectedValue(conflictError())
    render(
      <EntryEditor
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
    fireEvent.click(screen.getByRole("button", { name: "Save with If-Match" }))

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
    const view = render(
      <EntryEditor
        {...props}
        currentVersionId="version-1"
        versionId="version-1"
      />,
    )

    fireEvent.change(screen.getByLabelText("Entry title"), {
      target: { value: "My draft based on version one" },
    })
    view.rerender(
      <EntryEditor
        {...props}
        currentVersionId="version-2"
        initialDraft={{
          ...props.initialDraft,
          title: "Another session's title",
        }}
        versionId="version-2"
      />,
    )

    expect(await screen.findByRole("dialog")).toHaveTextContent(
      "newer server version arrived",
    )
    expect(screen.getByRole("dialog")).toHaveTextContent(
      "My draft based on version one",
    )
    fireEvent.click(screen.getByRole("button", { name: "Keep editing" }))
    fireEvent.click(screen.getByRole("button", { name: "Save with If-Match" }))

    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith(
        expect.objectContaining({ title: "My draft based on version one" }),
        "version-1",
      ),
    )
  })
})

import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import type { MePublic } from "@/client"
import type { ClinicalComment } from "@/features/api"
import { clinicalApi } from "@/features/api"
import { CommentsRail } from "./CommentsRail"

const resolvedComment: ClinicalComment = {
  id: "11111111-1111-4111-8111-111111111111",
  entry_id: "22222222-2222-4222-8222-222222222222",
  entry_version_id: "33333333-3333-4333-8333-333333333333",
  parent_id: null,
  author_id: "44444444-4444-4444-8444-444444444444",
  body: "Please confirm the updated care plan.",
  anchor_state: "resolved",
  review_required: false,
  assigned_membership_id: null,
  revision: 1,
  mentioned_user_ids: [],
  resolved_at: "2026-08-28T00:00:00Z",
  created_at: "2026-08-27T00:00:00Z",
}

const currentUser = {
  user_id: "55555555-5555-4555-8555-555555555555",
  membership_id: "66666666-6666-4666-8666-666666666666",
  role: "clinician",
} as MePublic

describe("CommentsRail resolution workflow", () => {
  it("lets a collaborator reopen a resolved discussion", async () => {
    vi.spyOn(clinicalApi, "comments").mockResolvedValue([resolvedComment])
    vi.spyOn(clinicalApi, "teamMembers").mockResolvedValue([])
    const unresolve = vi
      .spyOn(clinicalApi, "unresolveComment")
      .mockResolvedValue({ ...resolvedComment, resolved_at: null })
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })

    render(
      <QueryClientProvider client={queryClient}>
        <CommentsRail
          currentUser={currentUser}
          entryId={resolvedComment.entry_id}
          entryVersionId={resolvedComment.entry_version_id}
        />
      </QueryClientProvider>,
    )

    expect(
      await screen.findByText("Please confirm the updated care plan."),
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Reopen" }))

    await waitFor(() => expect(unresolve).toHaveBeenCalledOnce())
    expect(unresolve.mock.calls[0]?.[0]).toBe(resolvedComment.id)
    expect(unresolve.mock.calls[0]?.[1]).toBe(resolvedComment.revision)
  })
})

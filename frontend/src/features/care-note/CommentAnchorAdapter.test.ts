import { Editor } from "@tiptap/core"
import StarterKit from "@tiptap/starter-kit"
import { afterEach, describe, expect, it, vi } from "vitest"

import {
  createCommentExtension,
  markCommentSelection,
  selectionToCanonicalAnchor,
} from "./CommentAnchorAdapter"

let editor: Editor | undefined

afterEach(() => {
  editor?.destroy()
  editor = undefined
})

describe("Serene comment extension adapter", () => {
  it("loads with the pinned Tiptap 3 packages and serializes a selection", () => {
    const onActivated = vi.fn()
    editor = new Editor({
      extensions: [StarterKit, createCommentExtension(onActivated)],
      content: {
        type: "doc",
        content: [
          {
            type: "paragraph",
            content: [{ type: "text", text: "疼痛🙂 improved" }],
          },
        ],
      },
    })
    // ProseMirror positions are UTF-16 offsets, so the emoji occupies two.
    editor.commands.setTextSelection({ from: 1, to: 5 })

    expect(selectionToCanonicalAnchor(editor)).toMatchObject({
      exact_quote: "疼痛🙂",
      start_offset: 0,
      end_offset: 3,
    })
    expect(markCommentSelection(editor, "comment-1")).toBe(true)
    expect(editor.getHTML()).toContain('data-comment-id="comment-1"')
  })

  it("preserves the backend source representation for CRLF and NFD text", () => {
    editor = new Editor({
      extensions: [StarterKit, createCommentExtension()],
      content: {
        type: "doc",
        content: [
          {
            type: "paragraph",
            content: [{ type: "text", text: "Plan\r" }],
          },
          {
            type: "paragraph",
            content: [{ type: "text", text: "cafe\u0301 review" }],
          },
        ],
      },
    })
    editor.commands.setTextSelection({ from: 8, to: 13 })

    expect(selectionToCanonicalAnchor(editor)).toEqual({
      exact_quote: "cafe\u0301",
      start_offset: 6,
      end_offset: 11,
      prefix: "Plan\r\n",
      suffix: " review",
    })
  })
})

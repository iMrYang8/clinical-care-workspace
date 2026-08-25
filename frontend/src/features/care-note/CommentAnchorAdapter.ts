import { CommentExtension } from "@sereneinserenade/tiptap-comment-extension"
import type { Editor } from "@tiptap/core"

import {
  type CanonicalAnchor,
  canonicalizeText,
  codePointLength,
} from "./anchors"

export type ActiveCommentHandler = (commentId: string | null) => void

export function createCommentExtension(
  onCommentActivated: ActiveCommentHandler = () => undefined,
) {
  return CommentExtension.configure({
    HTMLAttributes: {
      class: "care-note-comment-anchor",
    },
    onCommentActivated,
  })
}

export function selectionToCanonicalAnchor(editor: Editor): CanonicalAnchor {
  const { from, to, empty } = editor.state.selection
  if (empty) {
    throw new RangeError("Select care-note text before creating a comment")
  }

  const documentEnd = editor.state.doc.content.size
  const content = canonicalizeText(
    editor.state.doc.textBetween(0, documentEnd, "\n", "\n"),
  )
  const before = canonicalizeText(
    editor.state.doc.textBetween(0, from, "\n", "\n"),
  )
  const selected = canonicalizeText(
    editor.state.doc.textBetween(from, to, "\n", "\n"),
  )
  const startOffset = codePointLength(before)
  const contentPoints = Array.from(content)
  const exactQuote = selected
  const endOffset = startOffset + codePointLength(exactQuote)

  return {
    start_offset: startOffset,
    end_offset: endOffset,
    exact_quote: exactQuote,
    prefix: contentPoints
      .slice(Math.max(0, startOffset - 32), startOffset)
      .join(""),
    suffix: contentPoints.slice(endOffset, endOffset + 32).join(""),
  }
}

export function markCommentSelection(
  editor: Editor,
  commentId: string,
): boolean {
  editor.commands.setComment(commentId)
  return editor.isActive("comment", { commentId })
}

export function clearCommentMark(editor: Editor, commentId: string): boolean {
  return editor.chain().unsetComment(commentId).run()
}

import { describe, expect, it } from "vitest"

import {
  currentOtherEditors,
  editorPresenceFromDomainEvent,
  editorPresenceRecordFrom,
} from "./editorPresence"

const record = {
  clinic_id: "clinic-1",
  patient_id: "patient-1",
  entry_id: "entry-1",
  entry_version_id: "version-1",
  actor_id: "actor-1",
  actor_role: "clinician",
  actor_display_name: "Dr Lee",
  expires_at: "2099-01-01T00:00:00Z",
} as const

describe("editor presence projection", () => {
  it("accepts only the scoped heartbeat event contract", () => {
    expect(
      editorPresenceFromDomainEvent({
        id: 7,
        event: "editor_presence",
        data: {
          aggregate_type: "entry",
          aggregate_id: "entry-1",
          payload: record,
        },
      }),
    ).toEqual(record)
    expect(
      editorPresenceFromDomainEvent({
        id: 8,
        event: "entry.updated",
        data: {
          aggregate_type: "entry",
          aggregate_id: "entry-1",
          payload: record,
        },
      }),
    ).toBeNull()
    expect(
      editorPresenceFromDomainEvent({
        id: 9,
        event: "editor_presence",
        data: {
          aggregate_type: "entry",
          aggregate_id: "different-entry",
          payload: record,
        },
      }),
    ).toBeNull()
    expect(
      editorPresenceFromDomainEvent({
        id: 10,
        event: "editor_presence",
        data: {
          aggregate_type: "patient",
          aggregate_id: "entry-1",
          payload: record,
        },
      }),
    ).toBeNull()
    expect(
      editorPresenceRecordFrom({
        ...record,
        actor_role: "admin",
      }),
    ).toBeNull()
    expect(
      editorPresenceRecordFrom({ ...record, actor_display_name: null }),
    ).toBeNull()
  })

  it("deduplicates actors and removes self, expired, and other-version records", () => {
    const visible = currentOtherEditors(
      [
        record,
        { ...record, actor_id: "self" },
        { ...record, actor_id: "expired", expires_at: "2000-01-01Z" },
        {
          ...record,
          actor_id: "other-version",
          entry_version_id: "version-2",
        },
        { ...record, actor_display_name: "Latest Dr Lee" },
      ],
      "version-1",
      "self",
      Date.parse("2026-09-02T00:00:00Z"),
    )
    expect(visible).toHaveLength(1)
    expect(visible[0]?.actor_display_name).toBe("Latest Dr Lee")
  })
})

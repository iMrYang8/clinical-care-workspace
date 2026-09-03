import { afterEach, describe, expect, it, vi } from "vitest"

import { installBrowserStorage } from "./browserStorage"

describe("browser storage in the test environment", () => {
  afterEach(() => {
    vi.restoreAllMocks()
    localStorage.clear()
    sessionStorage.clear()
  })

  it("leaves a runtime that already has working storage alone", () => {
    // The setup file installs storage before any test runs, so by now the
    // globals are usable and a second call must decline to touch them. This is
    // the branch CI takes under Bun, which never needs the replacement.
    const before = localStorage
    expect(installBrowserStorage()).toBe(false)
    expect(localStorage).toBe(before)
  })

  it("round-trips values and clears them", () => {
    localStorage.setItem("theme", "dark")
    sessionStorage.setItem("cursor", "42")

    expect(localStorage.getItem("theme")).toBe("dark")
    expect(sessionStorage.getItem("cursor")).toBe("42")
    expect(localStorage.length).toBe(1)

    localStorage.clear()
    expect(localStorage.getItem("theme")).toBeNull()
    expect(localStorage.length).toBe(0)
  })

  it("routes the global storage through Storage.prototype so failures can be simulated", () => {
    // Several suites make storage throw with `vi.spyOn(Storage.prototype, …)`.
    // That only reaches the components if the installed storage and the global
    // Storage class come from the same realm.
    expect(localStorage).toBeInstanceOf(Storage)

    const setItem = vi
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new Error("quota exceeded")
      })

    expect(() => localStorage.setItem("theme", "dark")).toThrow(
      "quota exceeded",
    )
    expect(setItem).toHaveBeenCalledWith("theme", "dark")
  })

  it("is accepted as a StorageEvent storage area", () => {
    // jsdom converts `storageArea` through its own IDL layer and rejects any
    // object it did not create, so a hand-rolled stand-in fails here.
    const event = new StorageEvent("storage", {
      key: "theme",
      newValue: "dark",
      storageArea: localStorage,
    })

    expect(event.storageArea).toBe(localStorage)
  })
})

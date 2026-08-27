import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { ThemeProvider } from "@/components/theme-provider"
import { Appearance } from "./Appearance"

describe("Appearance", () => {
  beforeEach(() => {
    window.localStorage.clear()
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      writable: true,
      value: vi.fn().mockReturnValue({
        matches: false,
        media: "(prefers-color-scheme: dark)",
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn(),
      }),
    })
  })

  it("exposes the current theme as a radio-menu selection", async () => {
    const user = userEvent.setup()
    render(
      <ThemeProvider defaultTheme="system" storageKey="appearance-test-theme">
        <Appearance />
      </ThemeProvider>,
    )

    await user.click(screen.getByRole("button", { name: "Appearance: system" }))

    expect(
      screen.getByRole("menuitemradio", { name: "System" }),
    ).toHaveAttribute("aria-checked", "true")
    expect(
      screen.getByRole("menuitemradio", { name: "Light" }),
    ).toHaveAttribute("aria-checked", "false")

    await user.click(screen.getByRole("menuitemradio", { name: "Dark" }))

    expect(
      screen.getByRole("button", { name: "Appearance: dark" }),
    ).toBeInTheDocument()
    expect(document.documentElement).toHaveClass("dark")
  })
})

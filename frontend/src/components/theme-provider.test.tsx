import { act, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { ThemeProvider, useTheme } from "./theme-provider"

const STORAGE_KEY = "theme-provider-test"

type MediaChangeListener = (event: MediaQueryListEvent) => void

const mediaListeners = new Set<MediaChangeListener>()
let systemPrefersDark = false

const installMatchMedia = () => {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: vi.fn().mockImplementation((query: string): MediaQueryList => {
      return {
        matches: systemPrefersDark,
        media: query,
        onchange: null,
        addEventListener: ((_type: string, listener: EventListener) => {
          mediaListeners.add(listener as MediaChangeListener)
        }) as MediaQueryList["addEventListener"],
        removeEventListener: ((_type: string, listener: EventListener) => {
          mediaListeners.delete(listener as MediaChangeListener)
        }) as MediaQueryList["removeEventListener"],
        addListener: (listener) => {
          if (listener) mediaListeners.add(listener)
        },
        removeListener: (listener) => {
          if (listener) mediaListeners.delete(listener)
        },
        dispatchEvent: () => true,
      }
    }),
  })
}

const setSystemPreference = (matches: boolean) => {
  systemPrefersDark = matches
  const event = { matches } as MediaQueryListEvent
  for (const listener of mediaListeners) listener(event)
}

const ThemeProbe = () => {
  const { resolvedTheme, setTheme, theme } = useTheme()

  return (
    <>
      <output data-testid="theme">{theme}</output>
      <output data-testid="resolved-theme">{resolvedTheme}</output>
      <button type="button" onClick={() => setTheme("dark")}>
        Choose dark
      </button>
    </>
  )
}

const renderProvider = (defaultTheme: "dark" | "light" | "system" = "system") =>
  render(
    <ThemeProvider defaultTheme={defaultTheme} storageKey={STORAGE_KEY}>
      <ThemeProbe />
    </ThemeProvider>,
  )

describe("ThemeProvider", () => {
  beforeEach(() => {
    mediaListeners.clear()
    systemPrefersDark = false
    installMatchMedia()
    window.localStorage.clear()
    document.documentElement.classList.remove("light", "dark")
    document.documentElement.removeAttribute("data-theme")
    document.documentElement.style.colorScheme = ""
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("uses the system preference by default and applies it to the document", () => {
    systemPrefersDark = true
    renderProvider()

    expect(screen.getByTestId("theme")).toHaveTextContent("system")
    expect(screen.getByTestId("resolved-theme")).toHaveTextContent("dark")
    expect(document.documentElement).toHaveClass("dark")
    expect(document.documentElement).toHaveAttribute("data-theme", "system")
    expect(document.documentElement.style.colorScheme).toBe("dark")
  })

  it("accepts persisted themes and sends invalid persisted values to system", () => {
    window.localStorage.setItem(STORAGE_KEY, "dark")
    const firstRender = renderProvider("light")

    expect(screen.getByTestId("theme")).toHaveTextContent("dark")
    firstRender.unmount()

    window.localStorage.setItem(STORAGE_KEY, "sepia")
    renderProvider("light")

    expect(screen.getByTestId("theme")).toHaveTextContent("system")
  })

  it("updates a system theme when the operating-system preference changes", () => {
    renderProvider()

    act(() => setSystemPreference(true))

    expect(screen.getByTestId("resolved-theme")).toHaveTextContent("dark")
    expect(document.documentElement).toHaveClass("dark")
    expect(document.documentElement).not.toHaveClass("light")
  })

  it("persists an explicit selection and still applies it when storage fails", () => {
    const setItem = vi
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new DOMException("Storage is blocked")
      })
    renderProvider()

    fireEvent.click(screen.getByRole("button", { name: "Choose dark" }))

    expect(setItem).toHaveBeenCalledWith(STORAGE_KEY, "dark")
    expect(screen.getByTestId("theme")).toHaveTextContent("dark")
    expect(document.documentElement).toHaveClass("dark")
  })

  it("synchronizes valid theme changes from another tab", () => {
    renderProvider()

    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: STORAGE_KEY,
          newValue: "dark",
          storageArea: window.localStorage,
        }),
      )
    })

    expect(screen.getByTestId("theme")).toHaveTextContent("dark")
    expect(document.documentElement).toHaveClass("dark")
  })

  it("falls back safely when reading browser storage throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("Storage is blocked")
    })
    renderProvider("light")

    expect(screen.getByTestId("theme")).toHaveTextContent("light")
    expect(document.documentElement).toHaveClass("light")
  })

  it("reports use outside its provider", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined)

    expect(() => render(<ThemeProbe />)).toThrow(
      "useTheme must be used within a ThemeProvider",
    )
  })
})

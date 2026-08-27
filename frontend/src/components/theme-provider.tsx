import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
} from "react"

export type Theme = "dark" | "light" | "system"

type ThemeProviderProps = {
  children: React.ReactNode
  defaultTheme?: Theme
  storageKey?: string
}

type ThemeProviderState = {
  theme: Theme
  resolvedTheme: "dark" | "light"
  setTheme: (theme: Theme) => void
}

type ResolvedTheme = ThemeProviderState["resolvedTheme"]

const SYSTEM_THEME_QUERY = "(prefers-color-scheme: dark)"

export const isTheme = (value: unknown): value is Theme =>
  value === "dark" || value === "light" || value === "system"

const getSystemTheme = (): ResolvedTheme => {
  if (typeof window === "undefined" || !window.matchMedia) {
    return "light"
  }

  return window.matchMedia(SYSTEM_THEME_QUERY).matches ? "dark" : "light"
}

const readStoredTheme = (storageKey: string, fallback: Theme): Theme => {
  if (typeof window === "undefined") return fallback

  try {
    const storedTheme = window.localStorage.getItem(storageKey)
    if (storedTheme === null) return fallback
    return isTheme(storedTheme) ? storedTheme : "system"
  } catch {
    return fallback
  }
}

const storeTheme = (storageKey: string, theme: Theme) => {
  try {
    window.localStorage.setItem(storageKey, theme)
  } catch {
    // Theme selection still applies for this tab when storage is unavailable.
  }
}

const applyTheme = (theme: Theme, resolvedTheme: ResolvedTheme) => {
  const root = window.document.documentElement
  root.classList.remove("light", "dark")
  root.classList.add(resolvedTheme)
  root.dataset.theme = theme
  root.style.colorScheme = resolvedTheme
}

const ThemeProviderContext = createContext<ThemeProviderState | undefined>(
  undefined,
)

export function ThemeProvider({
  children,
  defaultTheme = "system",
  storageKey = "vite-ui-theme",
}: ThemeProviderProps) {
  const validDefaultTheme = isTheme(defaultTheme) ? defaultTheme : "system"
  const [theme, setThemeState] = useState<Theme>(() =>
    readStoredTheme(storageKey, validDefaultTheme),
  )
  const [systemTheme, setSystemTheme] = useState<ResolvedTheme>(getSystemTheme)
  const resolvedTheme = theme === "system" ? systemTheme : theme

  useLayoutEffect(() => {
    applyTheme(theme, resolvedTheme)
  }, [resolvedTheme, theme])

  useEffect(() => {
    if (!window.matchMedia) return

    const mediaQuery = window.matchMedia(SYSTEM_THEME_QUERY)
    const handleChange = (event: MediaQueryListEvent) => {
      setSystemTheme(event.matches ? "dark" : "light")
    }

    setSystemTheme(mediaQuery.matches ? "dark" : "light")
    if (mediaQuery.addEventListener) {
      mediaQuery.addEventListener("change", handleChange)
    } else {
      mediaQuery.addListener(handleChange)
    }

    return () => {
      if (mediaQuery.removeEventListener) {
        mediaQuery.removeEventListener("change", handleChange)
      } else {
        mediaQuery.removeListener(handleChange)
      }
    }
  }, [])

  useEffect(() => {
    const handleStorage = (event: StorageEvent) => {
      if (event.key !== storageKey && event.key !== null) return

      if (event.newValue === null) {
        setThemeState(validDefaultTheme)
        return
      }

      setThemeState(isTheme(event.newValue) ? event.newValue : "system")
    }

    window.addEventListener("storage", handleStorage)
    return () => window.removeEventListener("storage", handleStorage)
  }, [storageKey, validDefaultTheme])

  const setTheme = useCallback(
    (nextTheme: Theme) => {
      const validTheme = isTheme(nextTheme) ? nextTheme : "system"
      storeTheme(storageKey, validTheme)
      setThemeState(validTheme)
    },
    [storageKey],
  )

  const value = useMemo(
    () => ({ theme, resolvedTheme, setTheme }),
    [resolvedTheme, setTheme, theme],
  )

  return (
    <ThemeProviderContext.Provider value={value}>
      {children}
    </ThemeProviderContext.Provider>
  )
}

export const useTheme = () => {
  const context = useContext(ThemeProviderContext)

  if (context === undefined) {
    throw new Error("useTheme must be used within a ThemeProvider")
  }

  return context
}

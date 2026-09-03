import "@testing-library/jest-dom/vitest"

import { cleanup } from "@testing-library/react"
import { afterEach } from "vitest"

import { installBrowserStorage } from "./browserStorage"

installBrowserStorage()

afterEach(() => cleanup())

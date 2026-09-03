/**
 * Make `localStorage` and `sessionStorage` work under Node as well as Bun.
 *
 * Node 22.4 and later define their own global `localStorage` and
 * `sessionStorage`. Without `--localstorage-file` these are inert placeholder
 * objects carrying no Storage methods at all. Vitest's jsdom environment merges
 * the jsdom window into `globalThis` rather than keeping it beside it — in this
 * environment `document.defaultView === globalThis` — so Node's placeholder
 * shadows jsdom's real storage and jsdom's own instances become unreachable.
 * Every test that clears storage then fails under Node while passing under Bun,
 * which is what CI runs.
 *
 * jsdom's `Storage` constructor is not callable and its prototype methods
 * require internal slots, so a working instance cannot be assembled by hand. A
 * hand-rolled stand-in is also not enough: `new StorageEvent("storage", {
 * storageArea })` runs jsdom's IDL conversion, which rejects anything it did not
 * itself create. So we build one more jsdom window purely to borrow its storage,
 * and re-point the global `Storage` class at that window's, keeping the
 * instances and the class from the same realm. Tests simulate storage failures
 * with `vi.spyOn(Storage.prototype, "setItem")`, and that has to intercept the
 * storage the components actually use.
 *
 * This installs nothing when the runtime already provides usable storage, so
 * the CI path under Bun is untouched.
 */

import { createRequire } from "node:module"

/** The only members this borrows; jsdom ships no type declarations. */
type DonorWindow = {
  localStorage: Storage
  sessionStorage: Storage
  Storage: typeof Storage
}

type JsdomModule = {
  JSDOM: new (
    markup: string,
    options: { url: string },
  ) => { window: DonorWindow }
}

const isUsable = (value: unknown): value is Storage =>
  typeof (value as Storage | null)?.clear === "function"

const define = (target: object, name: string, value: unknown): void => {
  Object.defineProperty(target, name, {
    value,
    writable: true,
    enumerable: true,
    // `vi.stubGlobal` and `vi.unstubAllGlobals` both need to redefine these.
    configurable: true,
  })
}

/**
 * Replace unusable global storage placeholders with real jsdom storage.
 * Returns true when a replacement was installed.
 */
export function installBrowserStorage(): boolean {
  const global = globalThis as Record<string, unknown>
  const broken = (["localStorage", "sessionStorage"] as const).filter(
    (name) => !isUsable(global[name]),
  )
  if (broken.length === 0) {
    return false
  }

  // Storage needs a non-opaque origin, so inherit the environment's own URL
  // rather than letting this window default to `about:blank`.
  const location = global.location as Location | undefined
  const url = location?.href ?? "http://localhost:3000/"
  // Required lazily: on a runtime whose storage already works this never runs.
  const { JSDOM } = createRequire(import.meta.url)("jsdom") as JsdomModule
  const donor = new JSDOM("", { url }).window

  for (const name of broken) {
    define(global, name, donor[name])
  }
  // Keep the class and its instances in one realm: a spy installed on the
  // global `Storage.prototype` must reach the storage installed above.
  define(global, "Storage", donor.Storage)
  return true
}

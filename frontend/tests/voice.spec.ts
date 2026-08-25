import { expect, test } from "@playwright/test"

test.use({ storageState: { cookies: [], origins: [] } })

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const wav = new Uint8Array(44 + 16_000 * 2 * 11)
    const view = new DataView(wav.buffer)
    const write = (offset: number, value: string) =>
      [...value].forEach((character, index) => {
        view.setUint8(offset + index, character.charCodeAt(0))
      })
    write(0, "RIFF")
    view.setUint32(4, wav.length - 8, true)
    write(8, "WAVEfmt ")
    view.setUint32(16, 16, true)
    view.setUint16(20, 1, true)
    view.setUint16(22, 1, true)
    view.setUint32(24, 16_000, true)
    view.setUint32(28, 32_000, true)
    view.setUint16(32, 2, true)
    view.setUint16(34, 16, true)
    write(36, "data")
    view.setUint32(40, wav.length - 44, true)

    class MockMediaRecorder extends EventTarget {
      static isTypeSupported(type: string) {
        return type.includes("webm") || type === "audio/mp4"
      }
      mimeType = "audio/wav"
      state = "inactive"
      timer: number | undefined
      start() {
        this.state = "recording"
        this.timer = window.setTimeout(() => {
          this.dispatchEvent(
            new BlobEvent("dataavailable", {
              data: new Blob([wav.slice(0, Math.floor(wav.length / 2))], {
                type: "audio/wav",
              }),
            }),
          )
        }, 20)
      }
      stop() {
        if (this.timer) window.clearTimeout(this.timer)
        this.state = "inactive"
        this.dispatchEvent(
          new BlobEvent("dataavailable", {
            data: new Blob([wav.slice(Math.floor(wav.length / 2))], {
              type: "audio/wav",
            }),
          }),
        )
        queueMicrotask(() => this.dispatchEvent(new Event("stop")))
      }
    }
    class MockAudioContext {
      createAnalyser() {
        return {
          fftSize: 512,
          getByteTimeDomainData(values: Uint8Array) {
            values.fill(128)
          },
        }
      }
      createMediaStreamSource() {
        return { connect() {} }
      }
      close() {
        return Promise.resolve()
      }
    }
    const track = { label: "Synthetic browser microphone", stop() {} }
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: async () => ({
          getAudioTracks: () => [track],
          getTracks: () => [track],
        }),
      },
    })
    Object.defineProperty(navigator, "onLine", {
      configurable: true,
      get: () => localStorage.getItem("voice-force-offline") !== "1",
    })
    Object.assign(window, {
      MediaRecorder: MockMediaRecorder,
      AudioContext: MockAudioContext,
    })
  })
})

test("voice capture keeps encrypted chunks across offline reload", async ({
  page,
}) => {
  await page.goto("/login")
  await page.getByRole("button", { name: "Continue as Clinician" }).click()
  await page
    .getByRole("link", { name: "Open care note for Alex Synthetic" })
    .click()
  await page.getByRole("link", { name: "Record visit" }).click()
  await page.getByLabel("Synthetic fixture transcript").check()
  await page.getByRole("button", { name: "Start recording" }).click()
  await expect(page.getByText("Recording", { exact: true })).toBeVisible()
  await expect(page.getByText("1/1 chunks acknowledged")).toBeVisible()
  await expect(
    page.getByRole("button", { name: "Stop & finalize" }),
  ).toBeVisible()

  await page.evaluate(() => {
    localStorage.setItem("voice-force-offline", "1")
    window.dispatchEvent(new Event("offline"))
  })
  await page.getByRole("button", { name: "Stop & finalize" }).click()
  await expect(page.getByText(/encrypted chunks remain/i)).toBeVisible()
  await page.reload()
  await expect(page.getByText("Encrypted uploads to recover")).toBeVisible()

  await page.evaluate(() => localStorage.removeItem("voice-force-offline"))
  await page.reload()
  await page.getByRole("button", { name: "Resume upload" }).click()
  await expect(page).toHaveURL(/\/voice\/.+\/review/)
  await expect(page.getByTestId("voice-review-mode")).toBeVisible()
})

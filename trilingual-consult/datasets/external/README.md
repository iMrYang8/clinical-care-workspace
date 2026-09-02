# External multilingual audio (capability, not Q6 gold)

There is still **no** public Malay–English–Hokkien *medical consult* speech set.
This folder is a cache for small Hugging Face slices used to check that the
sandbox can score **real wavs** when ASR is actually present.

Do not commit wavs. Do not clone full 18 GB dumps. Do not scrape LDC/IMDA.

| Set | Hugging Face id | What it can prove | What it is not |
|---|---|---|---|
| ViMedCSS | `tensorxt/ViMedCSS` | Medical code-switching: Vietnamese matrix + English drug/term islands | Not Malay, not Hokkien |
| ASCEND | `CAiRE/ASCEND` | Spontaneous zh–en intra-utterance CS | Not medical consult |
| MultiMed | `leduckhai/MultiMed` | Multilingual *medical* ASR | Not necessarily code-switched; not SEA trilingual |
| Common Voice `nan-tw` | `mozilla-foundation/common_voice_17_0` config `nan-TW` | Read Taiwanese Hokkien smoke | Taiwanese ≠ Singapore Hokkien |
| AfriSwitchCare | `intronhealth/AfriSwitchCare` | Simulated CS consults (`[[EN]]` tags) | **Gated.** Skip on 401. Do not pirate |
| SEAME / IMDA NSC Part 4 | — | Best SEA CS speech | Out of tree. Licensed. Do not scrape |
| PriMock57 | already in Nightingale | English mock GP | Not a trilingual claim |

## Run

Default pytest never downloads.

```bash
cd trilingual-consult
uv sync --extra dev
uv run python -m trilingual_consult.audio_bench --set vimedcss --n 40
```

Needs the optional `audio` extra to stream HF (`uv sync --extra audio`). If
`LOCAL_ASR_MODEL_DIR` points at a local faster-whisper weight, clips are
decoded and WER/CER is written. Otherwise the artifact records
`ASR_UNAVAILABLE` and still runs agents on the **dataset transcript**.

Gated or missing sets write `skip_reason` and move on. That skip is the result.

Do not average ViMedCSS WER into a “Q6 SEA score”.

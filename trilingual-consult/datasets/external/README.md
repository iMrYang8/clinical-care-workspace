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

Do not average ViMedCSS WER into a "Q6 SEA score".

## What a real decoder changed

faster-whisper 1.2.1 (`Systran/faster-whisper-small`, int8, the version the
Nightingale backend locks) over macOS TTS of a gold Malay line:

```
gold  Dia ada alahan kepada penicillin masa kecil.
hyp   dia ada alahan kepada penicilin masa kecil.     ms, p=0.98
```

Language identification was right; the drug name lost a letter. That was enough
to make a clinician's English denial and a family's Malay report of the same
allergy two unrelated substances, so no conflict was raised and publication of
the denial was not blocked. Drug names are now recovered from a single edit,
bounded and always marked for review. See `tests/test_asr_misspelling.py`.

The lesson generalises past this one word: an alias table is a list of spellings
somebody guessed, and a decoder produces spellings nobody guessed.

## Honest scoring on public sets

The seven gold consults score 1.00 because the lexicon and the gold were written
together. Two public sets publish their own annotation, so scoring against them
is not circular:

| Set | Its own label | What we score with it |
|---|---|---|
| ViMedCSS | `cs_terms_list` per segment | Do annotated code-switched terms survive into the working text |
| ASCEND | per-utterance `language` of en/zh/mixed | Precision and recall of `MIXED_LANGUAGE_TURN` |

Review rate is deliberately **not** a metric here. Public clips carry no speaker
role, so review is forced on every fact and the rate is pinned near one.

## Known gaps in this runner

* `datasets` 5.x decodes audio through `torchcodec`, which is not installed, so
  audio-bearing runs report `AUDIO_DECODER_MISSING` and fall back to the
  dataset's own transcript. Text-only scoring is unaffected.
* AfriSwitchCare is gated. A 401 is recorded as the result, not worked around.

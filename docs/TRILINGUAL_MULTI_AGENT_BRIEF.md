# Q6 — The Trilingual Consult

**Malay, English and Hokkien inside one sentence, and everything downstream of that transcript**

Branch `feat/voice-multi-agent-pipeline` · 3 September 2026 · synthetic and public data only · flag off by default

---

## The question, and why the obvious answer is worthless

Q6 asks two things: what the transcript produces, and what everything downstream of that transcript produces. The second half is the one that matters, and no word error rate answers it.

There is no public Malay–English–Hokkien medical consult recording. Not gated, not expensive — it does not exist. SEAME is Mandarin–English and not clinical. IMDA's National Speech Corpus Part 4 has 900 hours of Singlish code-switching but is licensed and not medical. PriMock57 is English mock GP.

The tempting move is to write a gold set, run it, and report the score. We did write one: seven consults, and the pipeline scores **1.00 on all seven**. That number is worthless, and we say so on the artifact itself. The lexicon and the gold were authored together, so the lexicon is being graded on its own homework.

Everything below is organised around one constraint: **find evidence we did not author.**

---

## What we built

Seven agents in fixed order over in-memory state. No database. Proposals only — they never write a conflict case and never publish to a patient record.

```
Scribe → Attribution → Safety Sentinel → Term Anchor → Conflict → Summaries → Verifier
```

The division is not decorative. Attribution decides *who* spoke, and family hearsay is a different evidence class from a clinician's assertion. Term Anchor canonicalises drug names across languages so a nurse's `penicillin`, a patient's `青霉素` and a family member's `penisilin` become one key — without that step nothing downstream can find a contradiction. Verifier can block publication but cannot edit anything, because facts are frozen: a checker that rewrites its own input is not a checker.

Q6's real case is intra-sentential, so consult-07 puts all three languages in one sentence:

> `Dia [[NAN]] tui [[/NAN]] [[EN]] penicillin [[/EN]] [[NAN]] koe-bin [[/NAN]] masa kecil.`

It resolves `ms → nan → ms → en → ms → nan → ms` and yields one family-reported allergy, present, review required, no fabricated NKDA.

The whole path sits behind `VOICE_MULTI_AGENT_PIPELINE`, **false by default**, not in the 2 September freeze.

---

## What we tried, and what broke

### 1. The public benchmark had never returned a single clip

Its default set is ViMedCSS — 34 hours of Vietnamese medical code-switching, every utterance carrying an English medical term. Running it reported a clean pass over **zero clips, with no skip reason.**

ViMedCSS names its transcript column `segment_text`. The reader looked for six other names. Every row yielded an empty string and was dropped, and the summary was indistinguishable from a set that legitimately had nothing to say. That silence is why it survived. A zero result now carries `NO_USABLE_TRANSCRIPTS` and the row count. **After the fix: 30 clips, first time.**

### 2. The false-NKDA detector could not fire

The most safety-relevant metric in the codebase required an allergy fact that was both `absent` and had no named substance. The extractor cannot produce that pair. The same dead condition sat in the verifier, and two tests asserted against it and passed vacuously.

A denial is the dangerous direction: claiming an allergy costs a substitution, missing one can kill. The replacement fails closed on a blanket denial with no substance, and on a denial whose turn language never resolved. That second clause matters more than it looks — **a fact reports the language of the pattern that matched it**, so an English negation firing inside an unrecognised matrix language reports `en` and looks entirely trustworthy.

### 3. A real decoder found what no test could

Everything above still ran on transcripts the datasets provided. So we ran faster-whisper over TTS of our own Malay gold line:

```
gold  Dia ada alahan kepada penicillin masa kecil.
hyp   dia ada alahan kepada penicilin masa kecil.     ms, p=0.98
```

Language ID correct. The drug name lost one letter. The alias table happened to carry `penisilin` — a spelling somebody guessed — but not `penicilin`, the one the decoder actually produces. So it canonicalised to nothing and became a substance of its own:

| | before | after |
|---|---|---|
| Clinician, English: "not allergic to penicillin" | `penicillin, absent` | `penicillin, absent` |
| Family, Malay via ASR | `penicilin, present` | `penicillin, present` |
| **Conflicts detected** | **0** | 1 |
| **Publication blocked** | **no** | yes |

Two directly contradictory statements about one drug became two unrelated substances, and the denial was free to reach the patient record.

Enumerating misspellings cannot fix this — recognition errors are unbounded and the table was already guessing. Names are now recovered across a single edit, only when long enough that one edit is plausibly a slip and only when exactly one drug lies that close (`asprin` and `pen` stay unmatched). A recovered name is matched so the conflict becomes visible, then **never trusted**: the fact is forced to review and the consult carries `DRUG_NAME_RECOVERED_FUZZILY`.

### 4. A bigger model did not help

| | language ID | drug name | time |
|---|---|---|---|
| small | ms, 0.98 | `penicilin` ✗ | 2.2s |
| large-v3 | ms, **1.00** | `penicilin` ✗ | 11.3s |

Confidence went to a perfect score. Capitalisation improved. **The drug name failed identically at five times the compute.** This is the finding that justifies the design: if a bigger model fixed it, the recovery layer would be a workaround to delete later.

A sweep narrowed it further — `amoxicillin` and `aspirin` transcribe perfectly in the same Malay voice, so the error is **word-specific, not language-specific**. It also exposed two defects that fire on *completely correct* transcripts: Malay `alah kepada` without the `-an` suffix matches nothing, and a sentence naming two allergens yields only the first, in every language tested.

### 5. Adversarial tests designed to fail

Five pass, five fail. The failures are `xfail(strict=True)` with written reasons rather than deleted — a limit a reviewer can read beats a green suite that never went looking, and `strict` means fixing one forces its removal.

**Holds:** Hokkien `bo` and Malay `tiada` negation both read absent, though no gold consult covers either. Filler invents nothing. An unnamed denial stays `unknown`.

**Fails:** the Chinese patterns **hardcode the four penicillin aliases in their capture group** while every other language uses a generic one, so a patient naming any other drug in Chinese loses the substance entirely — their own language is the only one where that happens. Traditional script fails the same way. The generic groups are ASCII-anchored, so a Han-script drug name inside a Malay sentence is unreachable. English has no blanket-denial pattern, so `NKDA` yields nothing.

We also pinned the safe asymmetry: a **dropped** negator over-alerts, which costs a substitution, while the reverse hides a real allergy.

---

## What we can actually claim

Scored against annotations we did not write:

| measurement | value | source of truth |
|---|---|---|
| Code-switched terms surviving into working text | **33 / 33** | ViMedCSS, expert-annotated |
| Quote/offset integrity across 30 clips | **0 failures** | structural invariant |
| Fabricated NKDA | **0** | safety invariant |
| Sandbox tests | 57 pass, 5 xfail, 1 skip | — |

Zero quote-offset failures carries weight because these are Vietnamese with full diacritics, where character boundaries break most easily. Zero known-drug hits is honest, not a failure — the clips are about genetics and hormones and contain none of our four drugs. **Review rate is deliberately not reported:** public clips carry no speaker role, so review is forced on everything and the rate pins near 1.0.

**What none of it proves:** not Malay–English–Hokkien clinic performance; not a SEA score, because a Vietnamese word error rate is a Vietnamese word error rate; Common Voice `nan-TW` is Taiwanese, not Singapore Hokkien; the TTS is clean read speech, so the ASR findings are an **upper bound**; and none of it is clinical validation.

---

## Where we expect this to fail

**Hokkien needs a different implementation, not one more step.** The lexicon handles POJ romanisation and Whisper does not emit POJ — it produces Han characters, Mandarin, or hallucinated English. Even a perfect ASR integration does not open this path. It needs Han-character patterns, or a natively multilingual model such as A\*STAR's MERaLiON.

**Two defects fire on correct transcripts.** `alah kepada`, and multiple allergens in one sentence. "Allergic to penicillin and sulfa" is ordinary clinical language, not an edge case. These are worse than anything in the adversarial suite precisely because the transcript was right.

**The drug table is four entries and needs eleven registrations to extend** — alias map, English island regex, both Chinese captures, dose pattern and range, penicillin class plus a hand-copied duplicate in another agent, Hokkien cue, display name, benchmark regex. Every addition is an opportunity to half-add a drug.

**The fuzzy recovery has an unmeasured ceiling.** We know it recovers `penicilin` and `metfomin` and refuses `asprin`. We do not know its false-positive rate against a formulary of thousands, where the neighbourhoods are far more crowded.

**Public-set audio is still blocked.** `datasets` 5.x decodes through `torchcodec`, which is not installed. ViMedCSS survives on its own transcript column; ASCEND has none, so it returns nothing:

```json
{ "dataset": "ascend", "n": 0,
  "skip_reason": "AUDIO_DECODER_MISSING:...please install 'torchcodec'." }
```

Authenticating to the Hub did not change this, which rules out access and pins the cause on the decoder. That artifact is the first defect's fix paying for itself: before it, this run would have written nothing and reported a clean pass over zero clips.

---

## Assumptions, revisited

**Still standing.** That extraction must fail closed on an unresolved language rather than guess — the ViMedCSS run exercised exactly that path and fabricated no denials. That family hearsay is a distinct evidence class. That agents propose and never publish. That the flag stays off.

**No longer standing.**

*A bigger model is the answer to a transcription error.* `large-v3` matched `small` letter for letter on the failing word. Downstream repair is a requirement, not a stopgap.

*An alias table can cover what a decoder produces.* A table lists spellings somebody imagined; recognition produces spellings nobody imagined.

*A green suite means the path works.* The 1.00 was green throughout every defect above. The two most serious were found by running real audio and by trying to make the benchmark produce a number — not by testing.

*"No result" and "no data" are distinguishable by default.* The benchmark reported a clean zero for its own default dataset. Both that and the dead detector failed in the **flattering** direction, which is the direction nobody investigates.

---

## What we would do next, in order

1. **Fix the two correct-transcript defects.** Ordinary clinical language, both small.
2. **Make the drug registry single-source.** Eleven sites is the root cause behind both the Chinese hardcoding and the ASCII anchoring; it closes three `xfail` cases at once.
3. **Install `torchcodec` and rerun the public sets through a real decoder.** This converts term survival from a text property into survival *under real transcription* — the number this brief most wants and does not have.
4. **Measure the recovery ceiling** against a real formulary with generated ASR-plausible corruptions. If it degrades, gate on drug frequency or require two independent signals.
5. **Evaluate MERaLiON for the Hokkien leg** — in the sandbox, never in the default image.
6. **Last, more gold.** Adding `tiada alahan` and `bo tui X koe-bin` closes real coverage gaps, but a gold set we wrote ourselves adds coverage and never external validity. That is why it is last on a list that starts with a defect found by a decoder.

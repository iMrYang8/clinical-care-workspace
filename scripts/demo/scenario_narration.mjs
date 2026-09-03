/**
 * Narration cues for the per-scenario demo recordings.
 *
 * This is the single source of truth for scenario voiceover, exactly the way
 * `english_demo_content.mjs` is the source of truth for the twelve-minute demo.
 * `generate_scenario_captions.mjs` turns these cues into one SRT per clip and
 * `render_samantha_voiceover.py` speaks that SRT cue by cue, so the spoken line
 * and the subtitle line can never drift apart.
 *
 * `at` is an absolute offset in seconds from the start of that clip. Cue length
 * is derived, not declared: `max(3, words / 2.8)` seconds, capped at 7.5. The
 * generator refuses overlapping cues and any cue that runs past its own video,
 * so a wrong `at` fails the build rather than talking over the next shot.
 *
 * `videoSeconds` is the recorded length the cues were written against. The
 * generator probes the real file and refuses to build if the footage was
 * re-recorded to a different length — the narration then has to be re-timed.
 *
 * Every line here must be supported by what is actually on screen in that clip,
 * which is the same bar `record_scenarios.mjs --check` applies to `proofs`.
 */

export const narrations = [
  {
    id: "02-clinic-isolation",
    videoSeconds: 21.97,
    cues: [
      { at: 0.6, text: "The first clinic searches an MRN that belongs to another clinic." },
      { at: 5.0, text: "The record does not exist here: zero matching patient records." },
      { at: 9.0, text: "The same search inside her own clinic returns her." },
      { at: 13.0, text: "Platform oversight opens the clinic as a read-only view." },
      { at: 17.0, text: "Isolation is enforced in the database, not in the interface." },
    ],
  },

  {
    id: "05-clinic-b-onboarding",
    videoSeconds: 28.97,
    cues: [
      { at: 0.4, text: "A second clinic is configuration and data, not a new deployment." },
      { at: 4.7, text: "The administrator opens the onboarding dialog." },
      { at: 8.0, text: "The calibration gate is deliberately switched off." },
      { at: 11.4, text: "Preflight names the unmet requirement by reason code." },
      { at: 14.8, text: "Action required, with the exact gate that is missing." },
      { at: 18.4, text: "Restore the gate and run preflight again." },
      { at: 21.8, text: "Every check passes, and the status flips to Ready." },
      { at: 25.4, text: "Only then does the action become Create clinic." },
    ],
  },

  {
    id: "09-provider-outage",
    videoSeconds: 16.03,
    cues: [
      { at: 0.5, text: "The remote text processing provider is unavailable." },
      { at: 4.0, text: "Stored priorities remain visible, and are labelled." },
      { at: 7.5, text: "The card states when it was last generated." },
      { at: 11.0, text: "An outage degrades the feature, never the record." },
    ],
  },

  {
    id: "10-concurrent-edits",
    videoSeconds: 27.57,
    cues: [
      { at: 0.5, text: "Two care staff open the same saved note." },
      { at: 4.0, text: "Session A starts typing into its own draft." },
      { at: 7.5, text: "Session B edits the same note and saves first." },
      { at: 11.2, text: "Session A is warned before it can overwrite anything." },
      { at: 14.9, text: "Version conflict: nothing was silently merged." },
      { at: 18.4, text: "The losing draft is kept beside the latest saved note." },
      { at: 22.4, text: "No automatic merge was applied. A person decides." },
    ],
  },

  {
    id: "12-medication-gate",
    videoSeconds: 30.43,
    cues: [
      { at: 0.5, text: "Two source-linked medication instructions disagree." },
      { at: 4.2, text: "The record shows a clinical conflict, not a guess." },
      { at: 8.0, text: "Status: unresolved, at high severity." },
      { at: 11.5, text: "Each side opens the exact saved source behind it." },
      { at: 15.6, text: "The conflicting instruction is equally addressable." },
      { at: 19.6, text: "Neither source wins automatically." },
      { at: 23.2, text: "Staff cannot dismiss a high-risk conflict." },
      { at: 26.7, text: "Publication to the patient stays blocked." },
    ],
  },

  {
    id: "13-allergy-vs-nkda",
    videoSeconds: 106.13,
    cues: [
      { at: 0.5, text: "A nurse opens Alex Tan's record in the clinic workspace." },
      { at: 4.5, text: "She documents a named allergy to penicillin." },
      { at: 8.0, text: "It is saved as an ordinary care note." },
      { at: 12.0, text: "The same patient signs into her own portal." },
      { at: 15.6, text: "The patient channel is separate from the record." },
      { at: 19.2, text: "Nothing she writes here edits a clinician's note." },
      { at: 23.0, text: "Her account shows only what has been shared with her." },
      { at: 27.2, text: "There are no published entries and no shared highlights." },
      { at: 31.0, text: "Current priorities stays empty until the team publishes." },
      { at: 34.6, text: "Every patient and note in this recording is synthetic." },
      { at: 38.4, text: "She adds her own insight about her allergies." },
      { at: 42.0, text: "In her own words: I have no known drug allergies." },
      { at: 46.2, text: "The statement goes to the care team as hers." },
      { at: 50.0, text: "It appears on her timeline, attributed to her." },
      { at: 53.6, text: "The denial does not delete the nurse's note." },
      { at: 57.2, text: "Both statements are preserved, with their authors." },
      { at: 60.8, text: "A disagreement is never resolved by overwriting." },
      { at: 64.4, text: "A patient denial is evidence, not an erasure." },
      { at: 68.0, text: "The record keeps who said what, and when." },
      { at: 71.6, text: "Nothing is decided by whichever save happened last." },
      { at: 75.2, text: "The nurse's note and the patient's words both stand." },
      { at: 81.2, text: "The clinician now opens the same patient." },
      { at: 85.2, text: "Clinical conflicts shows a critical penicillin allergy." },
      { at: 88.8, text: "One side reads Source: staff, documented by the nurse." },
      { at: 92.2, text: "The other reads Source: patient, in her own words." },
      { at: 95.6, text: "Status: unresolved. Neither source wins." },
      { at: 99.0, text: "A blanket denial does not override an active named allergy." },
      { at: 102.9, text: "A clinician decides, and the record shows why." },
    ],
  },

  {
    id: "14-meaningful-numbers",
    videoSeconds: 26.33,
    cues: [
      { at: 0.5, text: "A confidence number should mean something." },
      { at: 4.0, text: "Current priorities carries source-supported items." },
      { at: 7.5, text: "This one says its confidence is not applicable." },
      { at: 11.0, text: "Why this decision explains what the item means." },
      { at: 14.5, text: "It states how it could be wrong, and what follows." },
      { at: 18.6, text: "Unqualified items stay in a protected review queue." },
      { at: 22.1, text: "That queue cannot be shared with the patient." },
    ],
  },
]

export const narrationById = new Map(narrations.map((item) => [item.id, item]))

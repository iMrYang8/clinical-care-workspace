export const SUBTITLE_LINE_LENGTH = 42;
export const SUBTITLE_MAX_LINES = 2;

export const segments = [
	{
		seconds: 25,
		role: "Care Staff · Patient Directory",
		title: "Find the right patient quickly",
		caption: "Today's visits · Previous records · Search",
		subtitles: [
			{
				at: 1,
				text: "Nightingale is a shared longitudinal record for coordinated clinical care.",
			},
			{
				at: 7,
				text: "Today's visits are separated from previous records for rapid triage.",
			},
			{
				at: 13,
				text: "Search covers name, MRN, and date of birth, with same-name warnings.",
			},
			{
				at: 19,
				text: "Every patient, note, recording, and evaluation shown here is synthetic.",
			},
		],
	},
	{
		seconds: 67,
		role: "Care Staff · Jordan Wong",
		title: "Ten-second priorities with exact sources",
		caption: "Verified priorities · Visible review queue · Exact source",
		subtitles: [
			{
				at: 1,
				text: "Jordan's header summarizes age, MRN, record span, notes, and conflicts.",
			},
			{
				at: 7,
				text: "The team sees twenty-two years of context without reading every note.",
			},
			{
				at: 13,
				text: "Current priorities contains at most five source-supported care items.",
			},
			{
				at: 19,
				text: "Acute oral-intake limits and glucose checks are visible immediately.",
			},
			{
				at: 25,
				text: "Unverified High or Critical items stay in the clinical review queue.",
			},
			{
				at: 31,
				text: "They remain prominent, but cannot be shared with the patient.",
			},
			{
				at: 37,
				text: "View source opens the precise saved version, not a similar summary.",
			},
			{
				at: 43,
				text: "The supporting sentence is highlighted with author and Singapore time.",
			},
			{
				at: 49,
				text: "Source status and version history are available in the same view.",
			},
			{
				at: 55,
				text: "This lets a clinician verify the claim before using it for care.",
			},
			{
				at: 61,
				text: "Supported items advance; uncertain items remain visible for review.",
			},
		],
	},
	{
		seconds: 85,
		role: "Care Staff · Alex Tan",
		title: "Document, collaborate, and request sharing",
		caption: "Staff note · Exact selection · Mention · Assignment · Request",
		subtitles: [
			{
				at: 1,
				text: "Care Staff can document observations, handovers, and follow-up actions.",
			},
			{
				at: 7,
				text: "They cannot overwrite the clinician-owned section of the record.",
			},
			{
				at: 13,
				text: "Add care note opens a focused dialog instead of shifting the page.",
			},
			{
				at: 19,
				text: "This follow-up note is saved as an immutable first version.",
			},
			{
				at: 25,
				text: "Its author role and Singapore timestamp appear in the timeline.",
			},
			{
				at: 31,
				text: "In Edit note, we select only the phrase about tomorrow's nurse call.",
			},
			{
				at: 37,
				text: "Comment on selection preserves the exact quote and surrounding context.",
			},
			{
				at: 43,
				text: "The comment mentions and assigns the active clinician by name.",
			},
			{
				at: 49,
				text: "Team discussion now shows the quote, comment, mention, and assignee.",
			},
			{
				at: 55,
				text: "Resolve and reopen actions retain the collaboration history.",
			},
			{
				at: 61,
				text: "Staff can request patient sharing, but cannot publish the note.",
			},
			{
				at: 67,
				text: "The request binds the exact saved version and enters Awaiting review.",
			},
			{
				at: 73,
				text: "Later edits cannot silently change what the clinician will approve.",
			},
			{
				at: 79,
				text: "The patient sees nothing until every publication gate passes.",
			},
		],
	},
	{
		seconds: 98,
		role: "Clinician · Jordan Wong",
		title: "Longitudinal context and AI-assisted notes",
		caption: "Three AI note types · Exact phrase · Add to priorities",
		subtitles: [
			{
				at: 1,
				text: "The clinician returns to Jordan's longitudinal timeline.",
			},
			{
				at: 8,
				text: "The active section is highlighted as the cursor moves through the page.",
			},
			{
				at: 15,
				text: "Records from 2026, 2025, 2021, and 2004 stay in one patient chart.",
			},
			{
				at: 22,
				text: "Older metabolic risk, diabetes, and pancreatitis remain available.",
			},
			{
				at: 29,
				text: "Human staff and clinical notes are clearly distinguished by role.",
			},
			{
				at: 36,
				text: "Three AI note types cover review, nursing handover, and patient account.",
			},
			{
				at: 43,
				text: "Each AI-assisted note retains a system role and immutable version.",
			},
			{
				at: 50,
				text: "We select the exact phrase stating vomiting and oral restriction.",
			},
			{
				at: 57,
				text: "Only a clinician may add that supported phrase to current priorities.",
			},
			{
				at: 64,
				text: "The confirmation dialog does not allow a free-form AI paraphrase.",
			},
			{
				at: 71,
				text: "A correction requires a new human-authored clinical entry.",
			},
			{
				at: 78,
				text: "The new priority still points to the same version and exact span.",
			},
			{
				at: 85,
				text: "Source-linked facts return normalized data to the original wording.",
			},
			{
				at: 92,
				text: "Unavailable provenance is routed to review, never presented as trusted.",
			},
		],
	},
	{
		seconds: 55,
		role: "Clinician · Jordan Wong",
		title: "Immutable versions, diff, and restore",
		caption: "Compare exact snapshots · Restore as a new version",
		subtitles: [
			{
				at: 1,
				text: "Change history lists every immutable version with author and time.",
			},
			{
				at: 8,
				text: "The first, second, and current snapshots remain independently readable.",
			},
			{
				at: 15,
				text: "Compare from and Compare to expose exact added and removed wording.",
			},
			{
				at: 22,
				text: "The clinician chooses an earlier snapshot and confirms Restore.",
			},
			{
				at: 29,
				text: "Restore creates a new current version instead of deleting later history.",
			},
			{
				at: 36,
				text: "The complete audit chain remains available after the real restore.",
			},
			{
				at: 43,
				text: "Concurrent edits require the caller's current version condition.",
			},
			{
				at: 50,
				text: "A stale write triggers review instead of silently overwriting care.",
			},
		],
	},
	{
		seconds: 61,
		role: "Clinician · Jordan Wong",
		title: "Resolve a clinical conflict safely",
		caption: "Two exact sources · Risk floor · Abstention · Correction",
		subtitles: [
			{
				at: 1,
				text: "Clinical review shows a High conflict between two care instructions.",
			},
			{
				at: 7,
				text: "One source is an earlier diabetes sick-day hydration plan.",
			},
			{
				at: 13,
				text: "The other limits oral intake during acute pancreatitis and vomiting.",
			},
			{
				at: 19,
				text: "Both exact sources remain visible; the model does not choose a winner.",
			},
			{
				at: 25,
				text: "Deterministic rules set floors for allergy, medication, and dose risks.",
			},
			{
				at: 31,
				text: "Route, frequency, and conflicting care plans also receive a risk floor.",
			},
			{
				at: 37,
				text: "A model may raise the risk, but it cannot lower that minimum.",
			},
			{
				at: 43,
				text: "Low confidence or unresolved risk causes abstention and blocks sharing.",
			},
			{
				at: 49,
				text: "Only a clinician can select a correction and provide a reason.",
			},
			{
				at: 55,
				text: "The conflict is resolved now; both sources and the decision remain audited.",
			},
		],
	},
	{
		seconds: 85,
		role: "Clinician → Patient · Alex Tan",
		title: "Approve, show, and withdraw patient content",
		caption: "Exact-version approval · Patient receipt · Audited withdrawal",
		subtitles: [
			{
				at: 1,
				text: "The clinician opens Alex's persisted patient-sharing request.",
			},
			{
				at: 7,
				text: "The card identifies the requester, request time, and review state.",
			},
			{
				at: 13,
				text: "Review exact version loads the snapshot Staff actually submitted.",
			},
			{
				at: 19,
				text: "It does not substitute a newer body that appeared during review.",
			},
			{
				at: 25,
				text: "Publication gates check provenance, redaction, and unresolved conflicts.",
			},
			{
				at: 31,
				text: "The clinician approves the exact version for patient sharing.",
			},
			{
				at: 37,
				text: "In the separate portal, Alex sees only the minimum approved content.",
			},
			{
				at: 43,
				text: "The receipt names the reviewer, approval date, and approved source.",
			},
			{
				at: 49,
				text: "Internal comments, raw AI output, risk scores, and models are excluded.",
			},
			{
				at: 55,
				text: "View approved source verifies the exact patient-visible version.",
			},
			{
				at: 61,
				text: "Back in the clinical workspace, the clinician selects Withdraw.",
			},
			{
				at: 67,
				text: "The patient refreshes the portal and the shared body is removed.",
			},
			{
				at: 73,
				text: "Withdrawal preserves approval, receipt, withdrawal state, and audit.",
			},
			{
				at: 79,
				text: "The team can still prove what was visible and when it was withdrawn.",
			},
		],
	},
	{
		seconds: 37,
		role: "Clinic Administrator",
		title: "Members, clinic AI settings, and audit",
		caption: "Clinic configuration · Read-only clinical access · Real events",
		subtitles: [
			{
				at: 1,
				text: "Clinic Admin manages members and clinic-level AI configuration.",
			},
			{
				at: 7,
				text: "Clinical content remains read-only for this administrative role.",
			},
			{
				at: 13,
				text: "Secrets show configuration state or last four characters, never the key.",
			},
			{
				at: 19,
				text: "Fast and advanced model slots can be configured by task sensitivity.",
			},
			{
				at: 25,
				text: "Activity log shows real actor, action, area, and Singapore time.",
			},
			{
				at: 31,
				text: "The audit record avoids copying clinical note bodies or credentials.",
			},
		],
	},
	{
		seconds: 55,
		role: "Bonus · Importance Learning",
		title: "Bounded, auditable clinic learning",
		caption: "Rule score + bounded feedback · Protected safety items",
		subtitles: [
			{
				at: 1,
				text: "Why this decision exposes the rule score and clinic adjustment.",
			},
			{
				at: 7,
				text: "The final importance value is reproducible from stored features.",
			},
			{
				at: 13,
				text: "This is bounded clinic feedback, not a language model or user profile.",
			},
			{
				at: 19,
				text: "Confirm, pin, comment, edit, and explicit dismiss reasons are events.",
			},
			{
				at: 25,
				text: "Only Not relevant and Outdated can affect non-critical ranking.",
			},
			{
				at: 31,
				text: "Too busy, no click, and a quick exit are not treated as negative labels.",
			},
			{
				at: 37,
				text: "Impressions record exposure for audit without directly changing rank.",
			},
			{
				at: 43,
				text: "Feedback events and feature statistics reconstruct each adjustment.",
			},
			{
				at: 49,
				text: "Critical, unresolved, and clinician-confirmed items stay protected.",
			},
		],
	},
	{
		seconds: 37,
		role: "Bonus · Historical Retention",
		title: "Data decay without deleting history",
		caption: "Hot record → encrypted archive → verified rehydration",
		subtitles: [
			{
				at: 1,
				text: "Eligible older bodies can move from hot storage to encrypted archive.",
			},
			{
				at: 7,
				text: "The backend compresses with zstd and encrypts with AES-GCM.",
			},
			{
				at: 13,
				text: "Version metadata, source links, checksums, and audit remain available.",
			},
			{
				at: 19,
				text: "Reading an archived note rehydrates it and verifies the checksum.",
			},
			{
				at: 25,
				text: "Critical, unresolved, pinned, or task-linked content stays protected.",
			},
			{
				at: 31,
				text: "The clinical page reports retention state but does not archive directly.",
			},
		],
	},
	{
		seconds: 49,
		role: "Bonus · Synthetic Voice",
		title: "Voice review with honest abstention",
		caption: "Speakers · Timestamps · Overlap · Exact audio source · Low",
		subtitles: [
			{
				at: 1,
				text: "The browser encrypts recording chunks before resumable upload.",
			},
			{
				at: 7,
				text: "Review mode preserves speaker, timestamp, language, and overlap.",
			},
			{
				at: 13,
				text: "Each extracted fact can jump to its transcript and audio interval.",
			},
			{
				at: 19,
				text: "Speaker segments remain reviewable rather than becoming hidden text.",
			},
			{
				at: 25,
				text: "PriMock57 mock consultations were evaluated with the configured model.",
			},
			{
				at: 31,
				text: "The measured result is Low, not upgraded for presentation.",
			},
			{
				at: 37,
				text: "Low or unavailable confidence keeps the output in human review.",
			},
			{
				at: 43,
				text: "The system makes no claim of validated clinical transcription.",
			},
		],
	},
	{
		seconds: 37,
		role: "Verification · Same Revision",
		title: "Automated evidence bound to this build",
		caption: "RBAC · Versions · Provenance · Sharing · Migrations · Browser",
		subtitles: [
			{
				at: 1,
				text: "The evidence shown here is bound to this Git revision and image.",
			},
			{
				at: 7,
				text: "Backend tests cover roles, immutable versions, and exact provenance.",
			},
			{
				at: 13,
				text: "They also cover concurrency, conflicts, abstention, and publication.",
			},
			{
				at: 19,
				text: "Frontend tests, type checking, and the production build are included.",
			},
			{
				at: 25,
				text: "Database migration and full browser gates run against the same build.",
			},
			{
				at: 31,
				text: "Negative evaluations remain Low or Unavailable in recorded evidence.",
			},
		],
	},
	{
		seconds: 29,
		role: "Nightingale",
		title: "Traceable decisions. Patient-safe delivery.",
		caption: "Supported → priorities · Uncertain → review · Safe → patient",
		subtitles: [
			{
				at: 1,
				text: "Risk, confidence, and importance are more than badges on a screen.",
			},
			{
				at: 7,
				text: "Each explains what it means, how it can be wrong, and what follows.",
			},
			{
				at: 13,
				text: "Supported information advances into current priorities.",
			},
			{
				at: 19,
				text: "Uncertain information keeps its source and waits for clinical review.",
			},
			{
				at: 25,
				text: "Only approved, patient-safe content reaches the patient portal.",
			},
		],
	},
];

export const TARGET_DURATION_SECONDS = segments.reduce(
	(total, segment) => total + segment.seconds,
	0,
);

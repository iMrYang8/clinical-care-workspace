# Nightingale Integration Plan

**How independently developed bundles are received, compared against the same base, and combined in one codebase**

Base `0c437fe` (exported as `9fffc01`) · September 2026 · governed by [`REVIEW_STANDARD.md`](REVIEW_STANDARD.md)

---

## 0 · Scope

This document sets out the procedure that follows the standard. The standard decides **what** is accepted; this plan decides **how** accepted changes enter the integrated codebase while preserving provenance, keeping migrations valid, and identifying conflicts.

When this document was written, no candidate version had been imported, no integration branch created, and no commit ported.

---

## 1 · The base

The base is the author's code before modification, exported as `nightingale-base.bundle`.

One fact affects later steps: the export was committed again, so it shares **no commit ID** with the author's repository even though the backend and frontend directories are identical in content. Changes in the author's codebase therefore cannot be extracted with `git log base..HEAD`; they follow **Path B** in Section 3 (no common commit, verified content correspondence).

Bundles from other candidate versions are received and recorded under Section 2, and their provenance is determined under Section 3.

---

## 2 · Intake

### 2.1 One intake record per candidate version

```
Candidate ID:
Submitter and time received:
Bundle file and SHA-256:
Advertised refs and candidate HEAD:            full commit ID
Declared base bundle SHA-256 and base commit:
Bundle type:                                  full | incremental
Additional refs or dependencies:
LFS, submodules, models, datasets:             content, version, location, access conditions
Runtime, migration and test instructions:
Licences and distribution restrictions:
Intake status:                                ready | materials_missing | invalid | provenance_pending
Issues, owner, deadline:
```

Calculate a checksum for the original bundle and archive it read-only. The checksum establishes that the file is the one received, not who wrote it, whether it works, or whether it is safe to run. Record each resubmission as a new intake record; do not silently replace the original.

### 2.2 Verify without executing candidate code

In a separate intake repository, use `git bundle verify` to check the format and prerequisite objects, `git bundle list-heads` to list refs, and `git fsck` to check object integrity. Do not run any installation, build, test or application scripts supplied by a candidate during intake. Do not touch any existing repository's working tree.

| Situation | Handling |
|---|---|
| Full bundle with clear refs | Restore in isolation, verify, and fix the candidate HEAD |
| Incremental bundle with prerequisite objects supplied | Verify and restore the correct base first, then import the increment; retain the full chain |
| Incremental bundle with prerequisite objects missing | Record `materials_missing` and list the missing commits; do not call the file corrupt |
| Format or object error | Record `invalid`, retain the error output and original file, and suspend use |
| Unclear refs or development starting point | Record `provenance_pending` and ask the submitter; do not infer the answer from branch names |
| Import succeeds but runtime materials are missing | Git intake is complete; record missing runtime evidence separately |

Intake failure is not functional failure. If required runtime evidence is still missing at the end of assessment, apply the missing-evidence rule in Section 1.4 of the standard.

---

## 3 · Provenance: three paths

First check for shallow clones and missing objects. Then look for common ancestors in an isolated environment containing the relevant objects. There may be more than one common ancestor; do not select one without recording the ambiguity.

**Path A: common commits exist.** Record the candidate's declared starting point, actual reachability and common ancestors. If the declared base does not match the history, ask for an explanation before claiming to have identified all contributions. Calculate each candidate's changes against **its own** base, then compare implementations of the same requirement. A direct diff between two candidate HEADs is an inspection aid, not a substitute for the contribution inventory.

**Path B: no common commits, but content correspondence is verified.** Compare complete trees and specified directories by object hash, and record the scope of the mapping. Separate differences introduced during export from the candidate's subsequent development. Extract changes against the candidate's actual base, then map them to the confirmed integration target. Do not invent parent–child relationships; `merge --allow-unrelated-histories` does not replace this analysis. **The author's own codebase follows this path.**

**Path C: neither common commits nor reliable content correspondence.** Record a different starting point. Obtain the base materials or agree on a new comparison base before proceeding. Ideas can be reviewed, but nothing is ported until provenance is clear.

---

## 4 · Change units

Assign each change unit a `CHG-ID` and record:

- The candidate version, its base, its HEAD, original commit set, and what was added, deleted, modified or retained
- The corresponding requirements, constraints, evidence, and actual change in user-facing behaviour
- The code, tests, configuration and migrations that must be adopted **together**; their order; mutually exclusive options
- Changes to interfaces, authentication, event fields, schemas, generated clients and dependency lockfiles
- Behavioural overlap or dependencies with other candidates, even where they did not change the same file

Build the dependency graph before deciding the batches. Taking a security check without its call sites, or a caller without its interface, does not constitute adopting a bounded module.

---

## 5 · Conflict types

| Type | What to check |
|---|---|
| Text | Edits to the same line; moves or renames; deletion by one side and modification by another |
| Behaviour and state | Changes in different files that affect the same state machine, permission or business rule |
| Interface | Requests and responses, authentication, event fields, call semantics, version compatibility |
| Data | Migration ID and parent conflicts; different meanings for the same field; historical data; indexes; constraints; backfills |
| Deployment | New dependencies; changed meanings of ports or configuration settings; runtime identities; shared state; resource or geographic requirements |

A clean text merge can still contain semantic conflicts. Run behavioural and integration tests after every port. A lack of Git conflicts is never sufficient evidence for acceptance.

---

## 6 · Integration target and porting method

Start the integration branch from the **confirmed common base**, not the author's `main`. Apply accepted changes in the order recorded under the standard. If the architecture comparison identifies another candidate as a better starting point, record that decision under I07 and restart the branch there.

| Relationship and change structure | Rule |
|---|---|
| Independent commit, clear prerequisites, compatible with the target | Use `cherry-pick -x`; verify each batch before continuing |
| Several features combined in one large commit | Split into reviewable units; record the source commit and selected scope; test again after splitting |
| Re-exported base with reliable content mapping | Port the candidate's patch against **its own** base; review context and export differences for each part; do not merge the entire history |
| Architecture cannot be ported directly | Accept the design and reimplement the necessary parts on the target; record “design adopted and rebuilt”; do not claim the original code was merged |
| Merge commit, empty patch or duplicate patch | Establish the parent commits, net change and content already in the target; do not default to the first parent or silently discard the patch |

Cherry-picking creates new commits. `-x` retains a source reference, but provenance must be checked manually after conflict resolution, rewriting or splitting. Original signatures do not automatically carry over to new commits.

Do not force-push. Do not rewrite a submitter's repository. Do not experiment in a working tree with uncommitted changes.

---

## 7 · Data migrations and interface compatibility

For each batch that changes data, test four cases: initialization of an empty database; upgrade from the confirmed base **with data**; retry after partial failure; and coexistence of old and new versions when O06 requires it. Passing an empty-database test does not establish that upgrades work.

Check migration dependencies, naming conflicts, field semantics, constraints, permissions, backfills, and the order of application reads and writes. Editing a migration file does not undo an applied migration. Use a reviewed new migration, data restoration or forward repair.

Changes to regions, roles, providers, models and resources are also compatibility changes. Map them back to the constraints in Section 4 of the standard; compilation alone is insufficient.

---

## 8 · Validation stages and provenance chain

Order: intake verification → relevant tests on the original candidate version → individual port tests → migration and interface tests → S01–S16, D01 and D02 on the combined code → fixed delivery version.

Candidates may run the complete standard in advance. Unchanged areas may cite evidence from the same version and environment, but dependency or configuration changes require revalidation. After integration, test affected paths, not just modified functions.

The provenance chain must be traceable in both directions:

```
Candidate ID → bundle checksum → original base / HEAD / commit set
→ change ID → accepted scope / reason → integration commit
→ resolved conflicts → requirement IDs → test run IDs / evidence
```

Distinguish three kinds of failure: failures already present in the candidate, failures introduced by an adapter, and failures introduced by integration. For each conflict resolution, record the original intent of each side, the final choice, the supporting requirement and the reviewer. Adopting one implementation from a candidate does not make that candidate responsible for all results of the integrated codebase.

---

## 9 · Recovery, stop conditions and delivery

Before each batch, record the last verified code and configuration version, database recovery point and task queue state. Plan code rollback, database recovery and external side-effect handling separately. A Git revert cannot recall sent messages or undo stored database rows.

**Stop** the batch if any of the following occurs: unclear provenance, missing required dependencies, migration damage, isolation failure or a critical regression. Preserve evidence and return to the last verifiable state or perform an approved forward repair. Confirm that backups are usable before restoring data.

**Delivery checklist:** standard and constraint versions; intake records; base mapping; change and dependency inventory; architecture decisions; provenance chain; conflict-resolution records; fixed code, configuration and dependencies; all results and each reason for failure; reviewer-disclosed preferences; every decision referred to Ira and its outcome.

# `docs/archive/` — historical, non-authoritative material

**Everything under this directory is archived. None of it is architecture
authority. None of it is a current roadmap.**

## Read this before opening anything here

1. **Do not read archived material by default.** If you are doing ordinary
   implementation, review, or product work, the files here are not part of your
   reading set. Start at `docs/architecture/README.md`.
2. **Archived text is not current.** Where an archived document says "current",
   it means current at the moment it was written. The single source for what is
   true now is `docs/architecture/0.5-implementation-status.md`.
3. **Archived research is not a roadmap.** A detailed, confident, thoroughly
   reviewed research document here may describe a path the project has since
   rejected. Length and rigour are not authority.
4. **Where archived text conflicts with an ACCEPTED ADR, the ADR wins.**
5. **Exception:** if an ACCEPTED ADR explicitly references a document, that
   document is not archived — it stays in the active tree. Nothing here is
   referenced by an ACCEPTED ADR.

## What is here

| Directory | Contents | Why archived |
| --- | --- | --- |
| `blocker-b-family-b/` | Family B / B-S1 task-history witness research (#1, #2A, #2A.1, #2B) | Superseded research path. NO-GO on B-S1 as the mutation-authority path. Not referenced by any ADR. |
| `postmortems/` | Concise postmortems of stopped work | Lessons preserved without forcing anyone to read the full research path. |
| `project-history/` | Verbatim project narrative extracted from active documents | History preserved out of the hot reading path. |

## Why material gets archived

A document is archived when **all** of the following hold:

- it is not an ACCEPTED ADR;
- no ACCEPTED ADR references it;
- it is not needed to do current implementation, review, or operations work;
- it records a completed, superseded, or abandoned path, or is primarily
  historical narrative.

A document is **not** archived merely because it is large, old, or about a
blocked area.

## What archiving does and does not do

- Archived files stay tracked in Git. History is preserved by `git mv`, not
  delete-and-recreate.
- Archiving changes **no** architecture authority and **no** ADR status.
- Archiving is not deletion. Deletion of historical material is a separate
  operator decision.

## The supersession ratchet

See `docs/architecture/README.md`, "Acceptance and supersession", for the
project rule that governs how accepted material becomes historical. In short:
committed artifacts are immutable historical facts, an accepted decision may be
explicitly superseded or revoked when a later witness falsifies its load-bearing
claim, and superseded text is moved and indexed as historical rather than
silently rewritten to pretend it was never accepted.

---
name: hubinet-test-gate
description: Validate evidence before declaring Hubinet Ops work done, ready, merge-safe, or release-ready. Use for PR completion, exact-head CI verification, Home Assistant compatibility, regression gates, Windows/Linux test questions, and final pre-merge checks.
---

# Hubinet Ops test and evidence gate

Use this skill before claims such as `DONE`, `READY`, `MERGE SAFE`, `release-ready`, or before an authorized merge.

This skill verifies evidence; it does not redefine architecture.

## Mandatory orientation

Read:

1. `AGENTS.md`;
2. `docs/architecture/0.5-implementation-status.md`;
3. the changed subsystem's accepted architecture when the change affects 0.5 semantics.

The implementation-status document records the current test baseline and known harness limitations.

## Establish exact scope

Before validating:

- identify repository and target branch;
- capture expected starting/base SHA when relevant;
- capture exact final/head SHA;
- inspect the exact diff/changed files;
- verify the worktree/branch state when local access exists;
- do not treat a Codex/user summary as proof of the diff or CI.

If the user or task specifies an expected head SHA and the remote moved, stop write/merge actions and report the race.

## Required repository validation

For runtime/code changes, expect the applicable repository gates defined by `AGENTS.md` and current CI (`.github/workflows/ci.yml`), including:

- Python compilation (`app`, `custom_components`, `tests`, `scripts`);
- relevant targeted pytest;
- full repository pytest when publishing/merging runtime changes;
- shell syntax validation;
- YAML parsing;
- tracked-runtime-file validation;
- `git diff --check` or equivalent whitespace validation.

Do not require a gate that no longer exists in the current tree/CI (e.g. the
retired managed-executor compilation or Docker deployment-smoke-sandbox
steps) merely because it was once mandatory — confirm the current gate list
against CI directly rather than from memory.

Do not weaken, skip, or rewrite tests merely to obtain green CI.

For docs/instructions/skills-only changes, use scoped validation appropriate to the files plus repository tracked-file/format checks where available. Do not invent meaningless runtime tests for Markdown-only changes.

## Exact Home Assistant gate

Current target from accepted status/architecture:

```text
Home Assistant Core 2026.8.1
Python >= 3.14.2
```

Repository CI pins `homeassistant==2026.8.1` and runs the native integration suite on Linux/Python 3.14.x.

When claiming HA compatibility:

- verify the workflow/job is associated with the exact final PR/head or merge-ref containing that head;
- verify the job reports Home Assistant 2026.8.1;
- verify Python is 3.14.x and satisfies the repository minimum;
- report the actual test count/result from the exact run.

## Native Windows limitation

Current implementation status records a known native Windows limitation:

```text
homeassistant.runner -> fcntl
```

If native Windows HA pytest stops before collection on POSIX `fcntl`:

- do not patch Home Assistant;
- do not patch system Python;
- do not install/fake `fcntl`;
- do not weaken the harness;
- use the exact Linux CI/devcontainer/WSL-class gate as compatibility evidence.

This limitation is not a product-code failure.

## PR finalization / race check

Before resolving final review threads or merging:

1. Re-fetch live PR metadata.
2. Confirm final HEAD is the reviewed/tested SHA.
3. Confirm required CI for that SHA/merge-ref is green.
4. Re-fetch unresolved review threads.
5. Confirm no newly arrived P1/P2 blocker exists inside the agreed review scope.
6. Respect current Draft/Ready state and explicit user authorization.

Do not trigger an additional broad review unless the user asks or the agreed workflow requires it.

## Merge safety

Only merge when explicitly authorized.

When the connector/API supports it, use an expected-head SHA guard so the merge fails if the PR moved after review.

A valid final report should include:

- exact final HEAD;
- changed-file/diff scope;
- targeted validation;
- full CI result where required;
- exact HA gate result where relevant;
- unresolved review-thread count;
- PR state;
- whether merge/deploy occurred.

Green tests prove the covered behavior passed; they are not a formal proof that no untested counterexample exists. Avoid claims stronger than the evidence.

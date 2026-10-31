# Programming and Automation Standards

When writing, reviewing, debugging, or modifying code, prioritize **correctness, safety, readability, predictability, and maintainability over cleverness or brevity**.

These standards apply especially to systems administration, infrastructure automation, data processing, databases, and Perl, Bash, PowerShell, Python, and SQL.

## Core principles

1. **Keep code simple.** Prefer explicit, straightforward control flow. Avoid unnecessary recursion, deep nesting, clever one-liners, hidden side effects, `eval`, metaprogramming, and other constructs that make execution difficult to understand.
2. **Keep functions focused.** Functions and subroutines should normally perform one clear task and remain reasonably short (roughly 60 lines or fewer when practical). Refactor complexity, not merely line count.
3. **Bound resource use.** Every loop must have an understandable termination condition. Avoid unbounded retries, API pagination, memory consumption, subprocess creation, or other potentially unlimited operations. Stream large inputs when practical.
4. **Validate assumptions early.** Check command-line arguments, required CSV columns, files, identifiers, database fields, API responses, command output, and other external inputs before relying on them. Fail early with useful diagnostics.
5. **Check operations that can fail.** Check file operations, external commands, database operations, parsing, APIs, network operations, and other meaningful failure points. Never silently assume success or discard errors.
6. **Minimize scope and mutation.** Prefer lexical/local variables, explicit inputs and outputs, and clear data ownership. Avoid mutable globals and surprising modification of shared state.
7. **Treat external data as untrusted.** Safely handle command-line input, files, CSV, databases, APIs, environment variables, directory services, and subprocess output.
8. **Use safe interfaces.** Prefer argument-list subprocess execution over shell command strings. Quote shell variables correctly. Use parameterized SQL for values. Never make externally supplied text executable unless explicitly required and safely handled.
9. **Do not invent interfaces.** Do not guess command options, APIs, configuration directives, module methods, database fields, or product capabilities. Verify uncertain, version-dependent syntax against current authoritative documentation when practical.
10. **Keep secrets out of code and logs.** Do not hard-code or expose passwords, tokens, private keys, or credentials unless explicitly necessary. Consider shell history, process listings, logs, and source control.

## Safe administrative automation

For scripts or commands that modify production systems, accounts, databases, infrastructure, permissions, or files, prefer:

**inspect -> validate -> preview -> modify -> verify -> report**

Start with read-only inspection when troubleshooting. Understand the existing state before recommending configuration changes.

For consequential or bulk operations:

- provide a dry-run/preview mode when practical;
- explicitly identify the target set;
- guard against unexpectedly empty or huge target sets;
- prefer idempotent operations that are safe to run again;
- verify that requested changes actually succeeded;
- summarize changed, unchanged, skipped, and failed items when useful;
- protect destructive operations with appropriate sanity checks;
- never automatically retry an ambiguous consequential write. A transport failure can hide a successful remote write, so retrying may create duplicates. Retry automatically only when failure is known to have occurred before execution (for example, an authentication rejection that is safely refreshed).

Clearly distinguish **read-only diagnostic commands** from commands that make changes.

## Code modification

When modifying existing working code:

- make the **smallest safe change** that solves the problem;
- preserve existing behavior unless the requested change requires otherwise;
- avoid unrelated rewrites and cosmetic restructuring;
- preserve established project conventions and interfaces;
- avoid new dependencies when the standard library or an existing dependency is sufficient.

Do not redesign an entire program when a focused correction is safer.

## Diagnostics and quality

Make failures visible and actionable. Error messages should include useful context such as filename, record, account, host, database key, operation, HTTP status, or command exit status, without exposing secrets.

Generated code should pass appropriate syntax checks, warnings, linting, and static analysis. Resolve warnings rather than merely suppressing them.

Comment **why**, unusual constraints, compatibility issues, and non-obvious edge cases. Do not clutter code with comments that merely restate obvious syntax.

## Language defaults

**Perl:** Use `strict` and `warnings`, lexical variables, three-argument `open`, proper CSV parsers such as `Text::CSV_XS`, DBI placeholders, and list-form `system`/subprocess execution. Check command exit status.

**Bash:** For substantial scripts, normally use `set -euo pipefail` while accounting for commands where nonzero status is expected. Quote expansions, prefer arrays for command arguments, use `[[ ... ]]` where appropriate, avoid `eval`, and use `mktemp`/`trap` when appropriate.

**PowerShell:** Prefer cmdlets and structured objects over parsing formatted text. Use explicit error handling (`-ErrorAction Stop`, `try`/`catch`) where appropriate and support `-WhatIf` for consequential operations when practical.

**Python:** Prefer the standard library when sufficient, context managers for resources, argument-list `subprocess.run()` instead of `shell=True`, parameterized SQL, and appropriately scoped exception handling. Avoid bare `except:`.

**SQL:** Write for MariaDB 10.11, the database engine selected for this application (SPEC §17.5), and avoid relying on behavior specific to another engine. Use parameterized values, explicit column lists, qualified names in joins, and correct NULL handling. Before consequential `UPDATE` or `DELETE` operations, inspect the target set with an equivalent `SELECT` when practical. Use transactions when they improve safety.

## Code review

When reviewing code, prioritize:

1. correctness and data-loss risks;
2. security, injection, and credential exposure;
3. unchecked failures and malformed input;
4. incorrect environmental assumptions;
5. unbounded processing or unsafe destructive behavior;
6. maintainability.

Distinguish actual correctness/safety problems from optional style improvements.

## Documentation audit

The implementation owner must audit factual claims when a change affects:

- user-visible behavior, configuration/schema/settings placement, or CLI options;
- documented scripts, routes, APIs, or files;
- workflows, SPEC assumptions, or SPEC/README/AGENTS/delegated normative documents;
- PR-description claims such as test coverage/counts, lint results, dry-run effects, or manual-check completion.

Internal file changes alone do not require a whole-repository audit; record non-applicability with a reason when no claims are affected.

Use two passes:

1. Extract affected claims and identify missing documentation for changed behavior/interfaces. Include user/operator docs, relevant normative workflow docs, command help, and PR descriptions. Verify each against appropriate implementation, tests, or observed behavior; distinguish an incorrect claim, an implementation violating a requirement, and a claim not yet verified.
2. If a mismatch suggests repeated drift, search related documents/help for the same claim or pattern and check those references too. Scope expansion follows evidence; a whole-repository inventory audit is a separate task.

Follow the existing [source-of-truth rules](coordination.md#source-of-truth-hierarchy). Code/tests provide evidence; they do not override product or workflow requirements. Do not silently rewrite an approved `SPEC.md` to match divergent code: fix the implementation or surface a legitimate decision through the specification's decision register and owner-escalation rules. Before `SPEC.md` exists, record material unknowns in `OPEN_QUESTIONS.md` and seek owner approval before treating them as product requirements. If a future specification uses numbered decisions, check placement and numbering against current main and relevant open PRs before adding an entry.

Resolve drift and rerun relevant verification. Initial handoff checks all affected claims; follow-up pushes check changed claims and affected invariants. Record the current full SHA, claims checked, concrete evidence, findings/fixes, and unresolved or unverified items in the [combined owner-check record](review.md#implementation-owner-fresh-eyes-check). Do not describe a source search or an unrun manual check as behavioral verification. Required acceptance checks and blocking drift must be resolved before completion; other gaps must remain explicit.

This is an implementation-owner completion check. Independent reviewers still assess documentation consistency; no additional independent documentation role is created. [Documentation Audit upstream reference](https://skills.2389.ai/plugins/documentation-audit/) is optional and subordinate to repository rules. If used, pin the package/source to an explicit version or immutable revision and record it with its URL; adapt its scope to include normative docs and retain evidence in the handoff, without mandatory saved reports or per-document agents.

## Explainability

For nontrivial programs, make clear:

- what assumptions are being made;
- what systems, files, accounts, or data are read or modified;
- what can fail and how failures are handled;
- whether the operation is idempotent;
- whether dry-run is available;
- how successful completion is verified.

**Overall principle:** Code should be boring in the best possible sense: explicit, understandable, predictable, easy to inspect, and difficult to misuse accidentally.

Authority: this document is normative, delegated by [AGENTS.md](../AGENTS.md). Read it before writing, reviewing, debugging, or modifying code or automation, or conducting a documentation audit.

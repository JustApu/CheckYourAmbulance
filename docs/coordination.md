# Coordination procedures

This document is normative, delegated by [AGENTS.md](../AGENTS.md). Read it before claiming, reviewing, handing off, taking over, escalating, or completing work. Model-tier rules remain in AGENTS.md.

GitHub is the coordination layer for Claude Code, Codex, ChatGPT, humans, and future coding agents. Coordination must be visible in issues, pull requests, review comments, and repository documentation.

## Source-of-truth hierarchy

- `SPEC.md`: approved product behavior and architecture (current version and approval record: SPEC §25.6 and §27). `REQUIREMENTS.md`, `OPEN_QUESTIONS.md` and `PROJECT_HANDOFF.md` are the historical discovery record and do not override it; open items are tracked in the SPEC §24 decision register.
- GitHub Issues: implementation scope, dependencies, acceptance criteria, and status.
- `AGENTS.md` and its delegated normative documents: mandatory engineering and coordination rules.
- `CONTRIBUTING.md`: concise contributor workflow and escalation protocol.
- Pull requests: implementation, review, and handoff record.

Product behavior in `SPEC.md` wins over an issue, and `AGENTS.md` and its delegated normative documents win for engineering/coordination rules. Do not invent settled behavior where the SPEC §24 decision register marks a question open. Correct stale sources rather than carrying contradictions forward.

## Coordination labels

Use these labels when they exist:

- `status: ready` — dependencies are satisfied and the issue is available to claim for implementation. For a partly startable issue, only the parts whose prerequisites are met are available; see [roadmap, milestones and dependencies](#roadmap-milestones-and-dependencies).
- `status: in progress` — an implementation agent has claimed the issue and active authoring/implementation is underway; an early draft PR before initial handoff remains `status: in progress`.
- `status: needs review` — implementation has been handed off in an open PR following owner Fresh Eyes checks and awaits independent review or follow-up re-review.
- `status: changes requested` — review has completed with blocking findings; the implementation owner is addressing findings or rework is required before re-review.
- `status: ready to merge` — implementation and independent review are complete for the current head, and awaiting owner merge; see [review status and merge readiness](review.md#review-status-and-merge-readiness).
- `status: needs owner` — a material decision or owner-only action is required.
- `status: blocked` — blocked by a non-owner dependency or external condition.
- `milestone` — marks a roadmap tracking issue; see [roadmap, milestones and dependencies](#roadmap-milestones-and-dependencies). Not an implementation claim.
- `agent: antigravity`, `agent: claude`, `agent: codex`, `agent: chatgpt`, `agent: human`, `agent: other` — current implementation owner. Add a more specific `agent: <name>` label later if a new contributor is used regularly.

Labels are a dashboard, not the sole lock. Some tools cannot manipulate labels. The signed claim record and the [claim-eligibility steps](#claiming-work), including explicit handoffs and valid stale takeovers, determine whether work is claimable; an early draft still protects an active claim. Because multiple agents may act through the same GitHub account, coordination comments MUST begin with a signed prefix such as `[Agent: Antigravity]`, `[Agent: Codex]`, `[Agent: Claude]`, `[Agent: ChatGPT]`, or `[Agent: Human]`.

## Roadmap, milestones and dependencies

This section is the normative home for how the roadmap, dependencies, parallel work and blocked work are tracked. What the product must do is defined by `SPEC.md` (its release plan is SPEC §2.1); GitHub Issues track delivery.

### Tracking issues

- The roadmap is a set of **tracking issues**, one per milestone (M0, M1, …) plus one for later releases, each carrying the `milestone` label. Find them with the label search `label:milestone`. Native GitHub Milestones are not used; if they are adopted later, the tracking issues remain the source of exit criteria.
- A tracking issue states the goal, **exit criteria**, a **child issue checklist**, what it depends on, what can run in parallel and which owner decisions it needs. It does not override `SPEC.md`.
- A tracking issue is **not claimable for implementation**. Work is claimed on the child issues. The owner, or a reviewing agent with the owner's agreement, closes a tracking issue by commenting links that show each exit criterion is met.
- Status labels on a tracking issue mean: `status: ready` = work in it may start; `status: in progress` = at least one child is in progress; `status: blocked` = waiting for a predecessor milestone; `status: needs owner` = only an owner decision remains. **Ready to start is not the same as unblocked to finish**: say in the issue when a milestone cannot close until an owner decision is made.
- Later milestones may be **placeholders**. Expand a placeholder into detailed child issues, as its own issue, only when its predecessor has stabilized, so the backlog does not go stale. Revising a roadmap issue means editing it with a dated revision note that names the issue or review that prompted it. A change to product behavior is a `SPEC.md` amendment (SPEC §25.4), never a roadmap edit.

### Child issues

Each child issue's body uses these headings: **Part of** (the tracking issue), **Goal**, **Acceptance criteria**, **Depends on**, **Parallelism**, **Owner decisions**, **Review**, and a recommended model tier. Write dependencies as `issue #N`. The parent/sub-issue relationship expresses hierarchy only; it does not express blocking.

### Dependencies, ready and blocked

- **Blocking is expressed by two things together:** the `Depends on` section of the dependent issue and its `status: blocked` label. The top-level `Depends on` lists the **whole-issue gates**: prerequisites that must be closed (merged) before **any** part of the issue can start. An issue with no parts is `status: ready` only when every issue in its `Depends on` is closed and no owner decision is needed to start.
- `status: needs owner` means an owner decision is required before the issue can proceed; use the [owner escalation format](#owner-escalation-format).
- **Partly startable issues.** An issue may be split in its body into **Part A / Part B** (or similarly named parts). The top-level `Depends on` then lists only the gates for starting any part, and each part lists its **own additional prerequisites** under its heading. Label the issue `status: ready` when it has no active claim and at least one part has all its prerequisites (top-level and its own) met, and state plainly which parts are available. **There is still exactly one active implementation owner per issue.** The claim comment names the part, the claimant works only that part, and may not start a part with an unmet prerequisite. Parts are completed **sequentially**: when the claimed part is finished, the owner posts a signed comment saying which part remains and either continues with the next part once its prerequisites are met or releases the issue (removing `status: in progress` and their `agent:` label when possible). Another agent may claim a remaining part only after that explicit release or handoff, or a valid stale takeover. **A PR for a non-final part uses `Refs #N`, not `Closes #N`; only the PR that completes the last part uses `Closes #N`.** If parts need different owners at the same time, split them into separate child issues, each with its own claim, branch and PR.
- **Unblocking duty:** when an implementation owner's PR closes an issue (or finishes a claimed part), that owner searches for issues that list it as a prerequisite, moves those whose top-level dependencies are now all closed from `status: blocked` to `status: ready`, and ticks the child checklist in the tracking issue. For a partly startable issue, post a signed comment that names each part that is newly available; **do not change an issue to `status: blocked` while it has an active claim**, and keep the remaining parts' status in that comment. If labels cannot be changed, post the signed comment alone.
- Before claiming, [claiming work](#claiming-work) step 2 still applies, to the part you claim: confirm every dependency of that part is merged or closed, whatever the labels say.

### Choosing work and working in parallel

- When several issues are ready, prefer, in order: an explicit owner priority comment; the earliest milestone with open exit criteria; the issue that unblocks the most others.
- Two pieces of work are parallel-safe only when **all direct and transitive prerequisites of each part being started are satisfied** (for example, two issues that both depend on the contracts issue are not parallel-safe until it has merged), they do not change the same files or require the same branch, and their `Parallelism` sections do not forbid it. A `Parallelism` section can narrow what runs in parallel but can never waive a dependency, the claim prerequisites, or [one writer per branch](#one-writer-per-branch). Parallel work on a shared interface (for example the data-model and interface contracts) starts only after that interface issue merges. The independent-review rules in [review procedures](review.md) apply to every parallel issue; do not open more concurrent PRs than can be reviewed, and do not stack a PR on another unmerged PR without stating the dependency in both PR bodies.
- Do not start stack-dependent work before the stack decision is recorded as approved by the owner.

## Claiming work

Before editing:

1. Read the issue, dependency list, comments, labels, and linked/open PRs.
2. Confirm required dependencies are merged/closed or explicitly satisfied. For a partly startable issue, confirm the prerequisites of the part you claim and name that part in your claim ([roadmap rules](#roadmap-milestones-and-dependencies)).
3. Do not claim work that already has a current claim comment, `status: in progress`, or an open implementation PR (an active claim on any part of a partly startable issue counts) unless the prior owner explicitly handed it off or the stale implementation takeover rule below applies. An early draft is an open implementation PR for this claim check; it does not make the issue freely claimable.
4. Add `status: in progress` and your `agent:` label if your access permits it.
5. Post a visible claim comment, for example: `[Agent: Codex] Claimed on branch issue-N-short-description. I am the implementation owner until the PR merges or I explicitly hand it off.`
6. Immediately re-read the issue comments. A **valid claim** is a signed claim posted when no earlier active, unwithdrawn valid claim or implementation PR existed, or after an explicit handoff or valid stale implementation takeover under the rules below. If another currently active valid claim has an earlier GitHub timestamp, that earlier claim wins. Withdraw your claim with a signed comment, remove your coordination labels when possible, and do not edit. This re-read is the tie-break for simultaneous claims.
7. Branch from current `main` as `issue-N-short-description`.

If labels cannot be changed, still post and verify the claim comment and proceed. Do not ask the owner merely to manipulate coordination labels.

## One writer per branch

Exactly one implementation agent owns a branch at a time.

- Other agents review through PR comments; they do not push competing fixes to the implementation branch.
- The implementation owner addresses review findings.
- Branch ownership changes after an explicit handoff comment naming the new owner, or through the stale-claim rule below.
- After either an explicit implementation handoff or a valid stale implementation takeover, the new owner must replace the prior owner's `agent:` label with their own on the linked issue and, if open, its implementation PR when access permits. Lifecycle status labels must be mirrored on the linked issue and implementation PR, as defined in [review procedures](review.md#review-status-and-merge-readiness). This ownership change does not change reviewer assignments; follow [review procedures](review.md) for review coordination.
- Before the initial review handoff, including while an early draft PR is open, a claim is stale after **48 hours with neither (a) a commit/push on the claimed branch nor (b) a signed issue or PR progress comment from the implementation owner**; measure from the claim or its latest such activity. An early draft protects the claim from ordinary competing claims but does not suspend this clock. Only a valid initial review handoff under [review procedures](review.md#review-status-and-merge-readiness) suspends staleness while waiting for review; a label or non-draft flag alone does not establish handoff. Returning a handed-off PR to draft does not restart the pre-handoff clock. After review findings are posted, takeover is allowed only if the implementation owner leaves them unaddressed for 48 hours with no branch commit/push or signed issue/PR progress comment. Activity by the shared GitHub account that is not attributable to the claiming agent does not refresh ownership. A replacement agent may post a signed `STALE CLAIM TAKEOVER` comment naming the prior owner, branch/PR, and inactivity evidence, then immediately re-read the issue/PR to ensure no earlier valid takeover exists, remove/replace coordination labels when possible, and continue on a fresh branch or clearly documented handoff branch. The earliest valid takeover timestamp wins.
- If the prior owner returns after a stale takeover, it must stop writing and coordinate through review/comments unless explicitly handed ownership back.
- An agent that intentionally abandons work should leave a handoff/status comment and remove its labels when possible rather than waiting for staleness.
- Independent issues may be worked in parallel; each gets its own branch and implementation owner.

## Cross-agent review

Read [review procedures](review.md) before claiming or conducting a review, handling review feedback, or recording merge readiness. That document is the sole normative home for review assignment, independence, follow-up, and readiness. **Agent and lifecycle status labels are mandatory on both the implementation issue and its PR.** Apply the implementation owner's `agent:` label to the PR at creation; a reviewer does not replace it with their own agent label. Exactly one matching `status:` lifecycle label **must** appear on each active implementation issue and its PR: `status: in progress` (draft or initial implementation underway), `status: needs review` (owner checks complete and independent review or re-review pending), `status: changes requested` (review completed with blocking findings and author rework is in progress), or `status: ready to merge` (required independent reviews complete for the current head, verification and documentation gates satisfied, awaiting owner merge authorization). Never use `status: ready` on a PR: it means an unclaimed issue. If a PR is genuinely blocked by an external dependency or requires an owner decision, use `status: blocked` or `status: needs owner` instead and explain in a signed comment. Do not use `status: ready to merge` simply because a reviewer approved an obsolete head.

**Lifecycle transitions follow [review status and merge readiness](review.md#review-status-and-merge-readiness) and must be mirrored on the linked issue and PR (the issue is authoritative if the labels temporarily differ).** The implementation owner normally performs the transition, while the reviewer may update readiness status after completing review: on PR creation label `agent: <implementer>` and `status: in progress`; after the signed current-head owner-check record and review handoff, replace status with `status: needs review`; after initial review handoff, when an independent review completes with blocking findings, replace status with `status: changes requested`; while in `status: changes requested`, intermediate pushes keep that status; after addressing findings and posting the updated owner checks on the new head, replace status with `status: needs review`; after qualifying reviews and readiness checks on the current head, replace with `status: ready to merge`; on loss of a readiness condition without review findings from `status: needs review` or `status: ready to merge` (such as a CI failure, merge conflict, or non-review push), revert to or remain in `status: needs review` while the implementation owner restores readiness; before initial handoff keep `status: in progress` (concrete bug reports on early drafts do not transition out of `status: in progress`). The reviewer posts findings and readiness without taking ownership of the implementation PR. On merge/close, remove obsolete lifecycle status labels from both surfaces where supported and close the linked issue when its acceptance criteria are complete. Update labels on both surfaces in the same work session as the signed comment; remove the previous `status:` label before or after adding the new one, and verify there is exactly one on each. If label tools are unavailable, explicitly note the label update as outstanding in the signed PR comment and ask the next capable agent to reconcile it. Signed claims and head-specific review records remain authoritative; labels are a discoverability dashboard, not permission to merge. For ownership changes, follow [One writer per branch](#one-writer-per-branch).

## Agent edit autonomy and owner permission boundaries

Agents have full autonomy to edit code on branches they own (claimed issues and the agent's own pull requests) without prompting the owner for permission each time. This grant does not alter [claiming work](#claiming-work), [one writer per branch](#one-writer-per-branch), handoff, or [review procedures](review.md); reviews and suggested fixes for other agents' branches are conducted through PR comments.

Installing project dependencies into the gitignored project-local `local/` directory (for example, a Python virtual environment under `local/` populated with `pip install`, from the project's pinned dependency manifest) is pre-approved.

Explicit owner permission is required before:
- Installing system packages, host utilities, or modifying software outside `local/`.
- Merging pull requests ([handoff and completion](#handoff-and-completion) item 6: the owner performs or explicitly delegates merges).
- Performing actions reserved to the owner in [when to contact the owner](#when-to-contact-the-owner) (credentials, billing, repository settings, external publication).

To request owner permission or decisions, use the [owner escalation format](#owner-escalation-format) with `status: needs owner`.

## When to contact the owner

Do not ask the owner for routine implementation choices. Choose the smallest safe option consistent with the sources of truth, document non-obvious assumptions, and continue.

Escalate with `status: needs owner` only when at least one is true:

- Approved `SPEC.md` (once present) is materially ambiguous or contradictory—or a pre-spec discovery decision is unresolved—and choices change user-visible behavior, stored data, security/privacy posture, or scope.
- An irreversible/destructive production action requires approval.
- Credentials, billing, account ownership, permissions, a merge/repository setting, or another owner-only action is required.
- A legal, licensing, privacy, or publication decision belongs to the owner.
- Two authoritative requirements conflict and cannot be reconciled safely; or the discovery baseline contains a material conflict that must be resolved before promotion to `SPEC.md`.
- Two active contributors have a scope/ownership conflict they cannot resolve from the GitHub record.

Do not escalate for reversible internal naming/design choices, diagnosable CI failures, ordinary review changes, dependency-documentation lookup, inability to manipulate labels, or any case where a safe documented choice exists within the issue and SPEC.

## Owner escalation format

When owner input is required, leave one concise issue or PR comment:

```text
OWNER INPUT REQUIRED

Decision or action:
<one sentence>

Why owner input is required:
<why the agent cannot safely decide or perform it>

Options:
A. <option and consequence>
B. <option and consequence>

Recommendation:
<preferred option and why>

Work that can continue meanwhile:
<what is not blocked, or "none">
```

For a purely manual action, replace `Options` with exact `Owner action` steps. Do not repeatedly ping the owner. Continue unblocked work and wait only on the blocked portion.

## Handoff and completion

Open implementation PRs as drafts and keep them drafts while implementation and owner checks proceed. Do not run or trigger remote CI during this work; draft PRs skip CI. The `checks` workflow listens to `synchronize` only to fail fast when a push reaches a ready PR; it skips the expensive checks for that event. Path filtering prevents Markdown-only PR events from reaching that guard. Return every ready PR to draft before pushing; for Markdown-only PRs this is a required author procedure that CI cannot enforce. At handoff, a PR with any non-Markdown path starts its applicable CI; a PR whose changed paths are all Markdown is filtered before jobs start and uses the local documentation verification and handoff record in [review procedures](review.md#documentation-only-handoff-verification). GitHub path filtering evaluates at most 3,000 changed files. Inspect rename paths locally because GitHub does not specify whether the old name participates in matching; for a rename with a non-Markdown side, verify the `checks` workflow ran at the PR head and manually dispatch it if not. For any PR over 3,000 changed paths, verify a run at the current head and manually dispatch if it did not start. Keep manual dispatch runs on a separate concurrency group from push and pull-request events, and confirm the run SHA matches the PR head SHA. Keep the issue `status: in progress` until the applicable initial review handoff; opening a draft does not bypass checks or release implementation ownership. If the PR is marked ready before its applicable handoff gate is satisfied, that is a workflow violation: restore draft status and retain `status: in progress` until a valid handoff. Labels and GitHub draft flags are not evidence of completed checks; temporary label-update delays during a valid handoff are corrected without repeating checks.

When handing off a PR for independent review:

1. Use `Closes #N`.
2. Complete local verification, the [owner Fresh Eyes check](review.md#implementation-owner-fresh-eyes-check), and applicable [documentation audit](engineering-standards.md#documentation-audit). For non-Markdown PRs, mark the PR ready to trigger its applicable CI and wait for those checks to pass on the current head. For qualifying Markdown-only PRs, record the local verification evidence required by [review procedures](review.md#review-status-and-merge-readiness); no remote workflow runs. Then include the combined signed evidence in the existing handoff comment and move the issue to `status: needs review`, removing `status: in progress` when possible. For fixes after findings, return the PR to draft before changing the branch, push updates while it is draft, and repeat this handoff gate before returning it to review.
3. Follow [review status and merge readiness](review.md#review-status-and-merge-readiness) for subsequent status transitions, evidence, and the stale implementation takeover exception.
4. Keep your `agent:` label until merge or explicit handoff.
5. Summarize verification, unresolved risks, and owner action still required.
6. **Agents do not merge PRs unless the owner explicitly authorizes that merge.** A reviewer may recommend a merge method, but the owner performs or explicitly delegates the merge.
7. After merge, issue closure is the completion signal; coordination labels may be removed as housekeeping.

## Periodic workflow ergonomics audit

Use Agent Ergo's intent as an occasional audit, not a gate on every feature PR. Consider an audit after material coordination/review-rule changes, new agent-family/tool onboarding, repeated coordination failures, or before a milestone closes (including v1) when evidence warrants it. A trigger prompts consideration, not an automatic policy rewrite.

Walk through how an arriving or returning agent orients, observes state, decides, acts, verifies, recovers, and learns. Cover the relevant journeys:

- Locate governing docs, bootstrap pinned dependencies, and distinguish permitted local installs from host changes requiring permission.
- Find the highest-priority eligible unclaimed ready issue, dependencies, and a race-free claim using the existing tie-break rules.
- Identify implementation ownership, current reviewer/follow-up reservations, and cross-family requirements.
- Determine required checks beyond CI, recover from failed CI/review, and distinguish stale approval from current-head review.
- Resume interrupted work without taking an active claim; reconcile concurrent specification/decision-register additions (once approved) and prior owner decisions.
- Establish true readiness and distinguish it from owner merge authorization.

Record observed friction and evidence, impact, the smallest proposed improvement, and an acceptance scenario in one audit issue/report. Distinguish a proposed design, a walkthrough, and runtime verification; readable conventions or metadata validation do not prove race-free claims. Create small follow-up issues only for actionable findings, prioritizing reuse, simplification, and removal over new tracking infrastructure. Assess whether the benefit justifies added lookups, comments, or rules. Reused findings need provenance, scope, and a correction path; remembered advice cannot override governing documents.

[Agent Ergo upstream reference](https://skills.2389.ai/plugins/agent-ergo/) is optional. If invoked, pin the package/source to an explicit version or immutable revision and record it with its URL in the audit report. Proposal-only audits produce findings without editing repository files. If the upstream package is invoked for such an audit, explicitly select its `audit-only` mode rather than its documentation-editing default; this is an upstream setting, not a repository command flag. Findings do not authorize implementation outside the claimed scope or bypass existing permission boundaries.

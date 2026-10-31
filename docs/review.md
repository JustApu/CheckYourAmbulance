# Review procedures

This document is normative, delegated by [AGENTS.md](../AGENTS.md). Read it before owner Fresh Eyes checks, claiming or conducting review, handling review feedback, or recording merge readiness. General implementation claims, handoffs, and owner escalation remain in [coordination procedures](coordination.md).

A reviewer must be a contributor other than the implementation owner. Passing CI does not replace review. Review assignment is coordinated on the PR; implementation ownership is claimed on the linked issue. For issue and PR labels, follow [coordination procedures](coordination.md#cross-agent-review). Reviewer claims do not replace the implementation-agent label. Shared GitHub account identity does not establish reviewer independence: use the signed agent identity and family in the PR record.

## Implementation-owner Fresh Eyes check

Before the initial independent-review handoff or marking a PR ready for review, the implementation owner must inspect the complete PR diff at the current head with relevant local verification. Open implementation PRs as drafts and keep them drafts while implementation or owner checks are in progress. Do not run or trigger remote CI during this work. At handoff, non-Markdown PRs run their applicable CI after owner checks and documentation audit are complete; qualifying Markdown-only PRs run no remote workflow and use the local verification gate below. Inspect acceptance criteria, logic and edge cases, security/input boundaries, validation and error recovery, resource bounds, data exposure, and whether passing tests actually prove the intended behavior. Apply these categories to the change's risk; documentation-only changes require review of the affected rules and journeys, not an unrelated code audit.

Use a skeptical perspective and, where practical, a fresh context to gain distance from implementation. A helper working for the implementation owner remains part of self-review. Neither Fresh Eyes nor the owner's documentation audit satisfies independent or cross-family review, and neither creates another reviewer role. Repository-native checks suffice; no helper or upstream package is required.

Fix findings, inspect those fixes, and rerun relevant verification before recording completion for the resulting full head SHA. For later handoffs after fixes, inspect the delta since the last recorded SHA and affected invariants; expand the pass when changes to scope, risk, or the base warrant it. Every handoff needs evidence for its current head. This does not carry forward independent approval of an older head.

Include one compact record in the existing signed handoff or findings-addressed comment, rather than separate gate comments:

```text
[Agent: <agent>] Owner checks for head <full SHA>; method: repository checklist.
Fresh Eyes: <initial full diff | delta from full SHA>; <concrete scope/checks and findings/fixes>.
Documentation audit: <claims and evidence, findings | not applicable with reason>.
Verification: <checks/results>; unresolved findings or unverified claims: <details | none>.
```

Name actual checks rather than only repeating categories. Disclose verification gaps and unresolved findings; blocking findings prevent completion. Recheck that the PR head matches the record before handoff. Reviewers verify this evidence alongside the existing Definition of Done and independently review the change.

[Fresh Eyes upstream reference](https://skills.2389.ai/plugins/fresh-eyes-review/) is optional. If invoked, pin the package/source to an explicit version or immutable revision and record it with its URL in the same evidence record. Repository rules override upstream commit-by-commit gates, ordering, fixed time budgets, and other conflicting defaults; use risk-scaled depth before handoff.

## Documentation-only handoff verification

A PR qualifies for the no-remote-CI path when it has at most 3,000 changed paths, every changed path is a Markdown file ending in `.md`, and no rename has a non-Markdown old or new path. The `checks` workflow path filter includes every path first, then excludes root and nested Markdown paths; GitHub documents that `*` does not match `/` and `**` does ([workflow path-pattern syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#patterns-to-match-file-paths)). Any other path starts the workflow. GitHub evaluates at most the first 3,000 changed files for path filtering, so a matching non-Markdown path beyond that limit can be missed ([path-filter diff limits](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow#git-diff-comparisons)).

Before handoff, inspect renames and count changed paths locally:

```sh
git diff --find-renames --name-status origin/main...HEAD
git diff --name-only --no-renames origin/main...HEAD | wc -l
```

GitHub documents that path filters use a three-dot diff, but does not specify whether the old filename of a detected rename participates in path matching. If a rename has a non-Markdown old or new path, verify that the `checks` workflow ran for the current PR head. If not, dispatch the full workflow against the PR head branch. For PRs with more than 3,000 changed paths, do the same regardless of the visible workflow status. Dispatch with `gh workflow run checks.yml --ref <pr-head-branch>`, find the run with `gh run list --workflow checks.yml --branch <pr-head-branch>`, and compare `gh run view <run-id> --json headSha` with `gh pr view <pr-number> --json headRefOid`. The SHA values must match before treating the run as verification for that head. This conservative check ensures code-to-document renames and oversized mixed changes receive CI even when path filtering does not start a run.

Before handoff, run the checks that apply to the changed documents and record the exact commands and results:

```sh
git diff --check
python3 -m unittest discover -s tools/tests -v
python3 tools/check_markdown_links.py
python3 tools/validate_scenarios.py
python3 tools/check_normative_registry.py
gitleaks git --redact --no-banner --timeout 300 .
```

Use Gitleaks v8.30.1, the version pinned and checksum-verified by the workflow; install it from the [official release](https://github.com/gitleaks/gitleaks/releases/tag/v8.30.1) and verify the platform archive against its published checksum. This command scans repository history locally and redacts findings in output. If `docs/data-model-contracts.md` changed, also run `python3 -m unittest discover -s tests -p 'test_contract_manifest.py' -v`. The tool tests verify the `AGENTS.md` registry, the scenario validator reads `SPEC.md` and `tests/TRACEABILITY.md`, and the Markdown link checker checks all tracked Markdown files. Audit factual claims and consistency with `SPEC.md`, and inspect the full diff for credentials, personal data and operational information under the repository's [data-hygiene rules](../AGENTS.md#github-data-hygiene). Report checks that do not apply or could not be run. Do not claim remote CI passed; none ran.

GitHub leaves workflow checks pending when path filtering skips a workflow. Do not configure a path-filtered workflow as a required status check; if repository protection settings later require it, revise this policy before enabling that requirement. See [GitHub's skip-workflow guidance](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/skip-workflow-runs).

## Reviewer families and independence

| Contributor | Reviewer family |
|---|---|
| Antigravity | Google |
| ChatGPT, Codex | OpenAI |
| Claude | Anthropic |
| Owner or another human | Human |
| Future tool/vendor | Its own family unless clearly equivalent to an existing family |

Ordinary low-risk changes may use a different contributor from the same family. ChatGPT reviewing Codex (or the reverse) is useful same-family review, not cross-family review. If the implementation used multiple families, name all of them; cross-family review must come from a family that did not implement the change. A different human reviewer is valid independent review regardless of the tools used by the implementer. Self-review never counts as independent review.

The following higher-risk changes require review by a different family from the implementer, or an independent human, before being considered ready to merge:

- security, authentication, or permissions;
- database transactions or migrations;
- destructive operations or pruning;
- backups or recovery;
- concurrency or locking;
- data-integrity-sensitive behavior;
- major architecture changes.

A same-family reviewer may still provide feedback or serve as primary reviewer, but that review alone does not satisfy this higher-risk gate. The implementer requests an additional cross-family/human review in a signed PR comment when opening a higher-risk PR without an assigned qualifying reviewer. A same-family primary reviewer must check that request at claim time and post it if missing. The human owner may assign, override, or resolve reviewer assignments; this does not itself waive the independent-review gate or authorize a merge. If no cross-family agent is available, request independent human review through the owner-escalation process. The owner may fulfill it with a signed `[Agent: Human]` review naming the full head SHA, checks, and findings; assignment or a waiver alone is not review and does not satisfy the gate. Human review still never authorizes a merge.

## Claiming a primary review

Before substantive review:

1. Read the issue, PR description, review/coordination comments, current head SHA, and any human assignment. Check the implementation families and risk category. Prefer an unclaimed PR over duplicating another active review. Before claiming initial or follow-up review, verify that the signed owner-check record names this exact full head SHA and includes the applicable checks. Missing or mismatched evidence means that head is not claimable yet; ask the implementation owner to refresh it rather than reviewing under the old record. Initial claims also require the handoff gate below; concrete bug reports remain permitted.
2. Do not claim the primary role if another reviewer’s valid active claim or follow-up reservation, or a completed review, already covers this head, unless the reviewer explicitly hands it off or the stale-review rule below applies. The implementer must not claim its own review.
3. Post a signed PR comment, for example: `[Agent: ChatGPT] Review claimed for head <full SHA>. Role: primary. Family: OpenAI. I will post findings or explicitly hand off; I normally re-review fixes.` State the implementation families and whether this is same-family, cross-family, or independent human review. If multiple sessions share an agent name, include a distinct synthetic reviewer identifier and retain it in subsequent comments.
4. Immediately re-read PR comments and the head SHA before starting. A valid claim has no earlier active, unwithdrawn valid claim or reservation held by another reviewer for that role/head and complies with any human assignment. The earliest GitHub creation timestamp wins; equal timestamps are resolved by earliest PR timeline position. A losing claimant posts a signed withdrawal and chooses other work. If the head changed, do not start review on either head under that claim. Recheck owner evidence for the new SHA and claim it through the follow-up procedure when eligible, rather than reviewing obsolete content.

Labels do not lock review ownership. If comments cannot be posted or the winning claim cannot be established, do not start a competing substantive review. Diagnose access or resolve the record through comments; contact the owner only under the existing escalation criteria.

An explicitly requested additional independent review uses `Role: additional` and identifies the request it fulfills. Claim/re-read/tie-break and stale rules apply separately to that requested review slot, so an additional reviewer does not displace the primary. Without a request, handoff, or stale takeover, other agents avoid duplicate full reviews. Anyone may still report a concrete Bug/risk they notice without claiming a full review.

## Findings, completion, and follow-up

The reviewer reads the issue, referenced `SPEC.md` sections when the specification exists (otherwise the applicable discovery requirements and open questions), `AGENTS.md`, applicable delegated rules, and the diff; separates **Bug/risk** findings from **Suggestion** items; and does not rewrite the implementation branch. The implementer remains responsible for resolving findings and keeping the PR description current.

Post findings or completion with the reviewed full head SHA, reviewer identity/family, independence classification, checks performed, unresolved Bug/risk findings, and whether another independent review is still required. Findings end the active review work claim, but reserve follow-up for that reviewer. Completed reviews apply only to the named head; optional suggestions alone do not block readiness.

A rebase or amend still needs current-head evidence: the original reviewer may use a targeted base/conflict check and `git range-diff` to establish that the reviewed changes are unchanged, instead of repeating the full review. The reviewer records that evidence and the new full SHA; implementer-only equivalence evidence cannot carry review forward because a changed base can affect behavior.

Every new implementation push invalidates the prior completed-review state and any active claim for the old head. The previous reviewer retains the follow-up reservation for its role on the new head, subject to the 48-hour rule below; this reservation permits that reviewer to claim the new SHA, not to keep reviewing the obsolete head. The original primary reviewer normally re-reviews the changed head, including fixes and their effect on prior findings, and posts a new signed claim with that SHA before resuming. The original additional reviewer likewise owns follow-up for its requested slot. Prior findings remain unresolved until explicitly addressed/rechecked or withdrawn; a new head does not erase them. Recheck the head immediately before posting completion or changing readiness: if it moved, record the old-head findings as historical and re-review the current head.

An explicit signed handoff names the role, new reviewer, and current head, and releases the old reviewer's assignment/reservation. The recipient posts and verifies a new claim before starting. Do not require the owner to assign routine reviews or re-reviews.

## Stale review claims and reservations

An active review claim is stale after **48 hours with no signed review-progress comment or posted findings from that reviewer for the claimed head**; measure from the claim or its latest such activity. A progress comment must identify the role and head SHA. Implementation pushes and unrelated activity through the shared account do not refresh reviewer activity. An old-head claim cannot authorize work on a new head.

While findings await implementation fixes, the follow-up reservation does not become stale merely because the implementer has not pushed a fix. After a new head appears, the original reviewer has 48 hours from that push, or its latest signed review-progress comment for that head, to resume/complete review. After that interval without reviewer activity, another reviewer may take over. A completed current-head review waiting only for merge does not become stale; a later push starts the follow-up interval again.

A replacement posts a signed `STALE REVIEW TAKEOVER` comment naming the role, current full head SHA, prior reviewer, and inactivity evidence, then immediately re-reads the comments/head. The same timestamp/timeline tie-break applies to competing valid takeovers. A returning reviewer must stop primary review work and coordinate through comments unless handed ownership back. Human assignments take precedence; unresolved conflicts use the existing owner-escalation process.

## Review status and merge readiness

Apply the same lifecycle status label to both the linked implementation issue and its PR; the issue is authoritative for work/claim state, while the PR mirrors it for discoverability. The implementation owner's `agent:` label is also mirrored on the PR. When either status changes, reconcile both surfaces in the same work session. A mismatch is a dashboard error, not a change in the signed claims or review state. `status: needs review` is the canonical label for review queues (the legacy synonym `status: review` is retired). Review claims and active review work are tracked through signed comments and do not introduce a separate status label, remaining in `status: needs review`. Draft PRs skip CI. The `checks` workflow listens to `synchronize` only to fail fast if a push reaches a ready PR; expensive jobs skip for that event. Path filtering prevents Markdown-only PR events from reaching that guard. Return every ready PR to draft before pushing; for Markdown-only PRs this is a required author procedure that CI cannot enforce. Complete local verification, Fresh Eyes, and the applicable [documentation audit](engineering-standards.md#documentation-audit) first. Then mark a non-Markdown PR ready for review to trigger applicable CI for that head; complete handoff after those checks pass. A qualifying Markdown-only PR starts no workflow and completes handoff using the local verification evidence above. Do not claim primary review before the applicable handoff gate; concrete bug reports on drafts remain welcome and leave the draft in `status: in progress`. If review findings require implementation changes, return the PR to draft before changing the branch, make and locally check fixes there, then mark it ready again only when owner checks and the documentation audit for the new head are complete; applicable CI runs for non-Markdown heads. While in `status: changes requested`, the issue and PR keep that status during draft fixes until the updated owner-check record and applicable verification pass on the delta head, which transitions them back to `status: needs review`. Use the following lifecycle:

| Event | Linked issue and PR status |
|---|---|
| Draft PR opens before initial handoff (including when concrete bug reports are posted) | `status: in progress` |
| Initial handoff after owner checks and applicable CI on the current head, or local docs-only verification for a qualifying Markdown-only head; primary review is claimed after that gate | `status: needs review` |
| Completed review with unresolved Bug/risk findings | `status: changes requested` |
| Intermediate push while addressing findings, before updated owner checks are posted | `status: changes requested` |
| New push addressing findings with updated owner checks and applicable CI, or local docs-only verification, on the delta head | `status: needs review` |
| Current head has completed review, the applicable independence gate is satisfied, no unresolved Bug/risk findings remain, applicable CI is green (or qualifying docs-only local verification is complete), and the PR is mergeable | `status: ready to merge` |
| Loss of a readiness condition without review findings (e.g. CI failure, base-branch merge conflict, or a non-review push from `status: needs review` or `status: ready to merge`) | `status: needs review` |
| External dependency blocks work, or an owner decision is needed | `status: blocked` or `status: needs owner` (with signed explanation) |
| Owner performs or explicitly authorizes merge | Closed after merge |

The implementation owner or reviewing agent updates the linked issue and PR together when these conditions change; remove the prior status label from both when setting the new one. A reviewer may update readiness labels but does not change the implementation owner's `agent:` attribution. After merge/close, remove stale lifecycle status labels from both surfaces when supported. A readiness transition must cite the current head and review evidence in a signed PR comment. Unknown CI/mergeability or incomplete independence evidence cannot establish readiness. No automatic merge is authorized by readiness.

Reviewers assign themselves through claims; owner input is needed only for unresolved assignment conflicts or another policy-required human decision. An issue waiting only for the owner to merge is not abandoned work and is not eligible for stale implementation takeover.

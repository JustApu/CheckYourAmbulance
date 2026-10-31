# Model Efficiency Guide

This document is **non-normative** operational guidance. The mandatory rule is in `AGENTS.md`: use the least expensive capability tier that can reliably complete the work, and escalate only when justified.

Model names, pricing, quotas, and availability change. Do not encode provider-specific names in normative workflow files. Update examples here when the tools available to the project change, following the linked-document update policy in [AGENTS.md](../AGENTS.md).

## Capability tiers

### Economy

Use for:
- issue triage and repository reconnaissance;
- documentation and mechanical edits;
- deterministic test additions;
- narrow lint/style fixes;
- repetitive transformations;
- simple PR review.

### Standard

Default for substantive engineering:
- normal feature implementation;
- nontrivial bug fixes;
- cross-file changes;
- config/parser/classifier/settings work;
- ordinary independent review.

### Frontier

Reserve for:
- genuinely difficult debugging;
- ambiguous architecture;
- security-sensitive design;
- subtle concurrency or transaction behavior;
- destructive/data-integrity/backup design;
- a problem where a reasonable standard-tier attempt has failed.

## Reasoning effort

When a tool exposes reasoning levels:
1. start low/medium for ordinary work;
2. increase reasoning before assuming a different model is required;
3. if repeated attempts fail, change tier or strategy instead of endlessly retrying the same configuration;
4. once the difficult part is solved, return routine implementation/tests/docs to standard or economy.

A stronger model at moderate reasoning may be more efficient than repeated failures from a cheaper model at maximum effort. Optimize for successful cost per task, not cheapest individual request.

## Project examples

- **Economy:** requirements editing, checklist-example fixtures, formatting, straightforward UI copy.
- **Standard:** inspection schedule logic, deficiency workflows, reporting filters, ordinary UI implementation.
- **Standard-high / strong review:** Google Workspace authorization, concurrent inspection writes, correction/invalidation history, deadline transitions.
- **Frontier when warranted:** unresolved architecture questions, difficult multi-user consistency bugs, historical compliance/data-integrity behavior.

## Independent review

[Review procedures](review.md) define the mandatory independence gate and higher-risk categories. Choose a review capability tier appropriate to the risk; the reviewer may reasonably use a higher tier than the implementer.

## Session/context efficiency

Use a fresh session per issue/worktree when practical. Durable context belongs in:
- `SPEC.md` once approved (until then: `REQUIREMENTS.md`, `OPEN_QUESTIONS.md`, `PROJECT_HANDOFF.md`);
- `AGENTS.md`;
- `CONTRIBUTING.md`;
- GitHub issues and comments;
- commits and PRs.

Avoid carrying unrelated chat/session history across issues.

## When the agent cannot choose its model

Some environments choose the model outside the agent session. In that case:
- do not stop;
- record the recommended tier in the issue or PR when useful;
- continue with the provided model;
- escalate only if the resulting capability is actually insufficient for the task.

## Premium-tier escalation note

For non-obvious frontier use, leave a short note such as:

`Model escalation: frontier — diagnosing ambiguous transaction behavior after a standard-tier attempt failed.`

This is for cost visibility, not owner approval.

## Maintaining current model mappings

If you want tool-specific recommendations (for example, which current Codex or Claude model maps to economy/standard/frontier), verify the provider's current documentation first. Keep those mappings here or in local operator notes, not in `AGENTS.md`.

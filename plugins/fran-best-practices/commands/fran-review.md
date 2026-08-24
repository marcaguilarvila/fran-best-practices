---
description: Review the current branch against the pre-PR ruleset before opening a PR
argument-hint: "[base-ref | PR number | paths]  (default: the repo's base branch)"
allowed-tools: Bash, Read, Grep, Glob, Edit
---

Run a pre-PR review of `$ARGUMENTS` (default: this branch against the repo's base branch) against
the standards in the `fran-best-practices` skill.

Use the skill's `references/rules.md` for the full rule detail — do not review from the
summary table alone.

Steps:

1. Resolve the diff. `$ARGUMENTS` may be a base ref, a PR number (`gh pr diff <n>`), or paths.
   With no argument, detect the base branch (`git remote show origin`) rather than assuming
   `main` — some repos target `develop`. Include uncommitted changes if the tree is dirty, and
   say so.
2. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/scan.py" --diff <base>` for the mechanical rules.
3. Verify every candidate against the real code before reporting it. Drop the ones the
   context justifies — a false positive costs more than a miss here.
4. Review by hand what the scanner cannot parse: R2 error layering and failure-path tests,
   R7 placement of upstream-specific logic, R8 field justification, R10 undefined jargon,
   and whether the tests changed shape alongside the code.
5. Report grouped by rule, BLOCK before ASK, each with `file:line`, the fix, and the quote
   that backs it. End with: **N blocking, M to justify** and whether he would approve.
6. Offer to apply the fixes. If the user accepts: fix each finding across the whole file, not
   just the flagged line; update the tests in the same change; and stage it as a separate
   `refactor(review): ...` commit rather than amending.

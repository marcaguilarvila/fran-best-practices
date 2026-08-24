---
description: Harvest a reviewer's new PR comments and grow the ruleset from them
argument-hint: "[--all | --repo owner/name]"
allowed-tools: Bash, Read, Edit, Write
---

Run the feedback loop for the `fran-best-practices` skill.

**Edits must land in the source checkout, never in `${CLAUDE_PLUGIN_ROOT}`.** That path is a
throwaway cache replaced on every reinstall — changes made there are lost and teammates never
see them. Resolve the real repository first:

```bash
SRC=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/source_root.py") || exit 3
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/source_root.py" --json    # branch, remote, dirty
```

If that exits 3, stop and relay its instructions — do not fall back to editing the cache.

Then:

1. `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/harvest.py" $ARGUMENTS` — prints the review comments
   absent from the catalog, each with its diff hunk and the commits pushed after it.
2. If nothing is new, say so and stop.
3. For each new comment, follow `$SRC/plugins/fran-best-practices/skills/fran-best-practices/references/feedback-loop.md`:
   is it a standard at all, does an existing rule cover it, or does it need a new `R<n>`?
   Read the file it anchors to and the follow-up commits first — the fix that got approved is
   what defines the rule, not the comment alone.
4. **Present each proposed rule change to the user before writing it.** The ruleset is shared;
   a wrong rule costs everyone.
5. Apply the accepted edits under `$SRC`: append to `catalog.json` (never invent or change an
   `id`), update `rules.md` and the table in `SKILL.md`, and add a `scan.py` check only when it
   can be detected mechanically without noise.
   **Anonymise as you write.** No real repository names, file paths, people or client systems
   reach `catalog.json` or `rules.md`: repos are opaque labels, paths reduce to the layer, and
   domain identifiers inside a quote are substituted for neutral equivalents without changing
   the objection. The only file naming anything real is `references/sources.json`, which is
   gitignored.
6. For any new mechanical check, verify the round trip: check out the commit he reviewed and
   confirm `scan.py` reports the finding at his line. Show the user that output.
7. Bump `last_harvest`. Commit in `$SRC` on a branch, and — only with the user's go-ahead —
   push and open a PR. Teammates pick it up on their next `claude plugin update`.
8. Remind the user to run `claude plugin update fran-best-practices` (or, from a local
   directory marketplace, `uninstall` + `install`) so their own copy reflects the change.

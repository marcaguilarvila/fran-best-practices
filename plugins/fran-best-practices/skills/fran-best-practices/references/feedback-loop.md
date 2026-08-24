# Feedback loop — how the ruleset grows

The ruleset is only useful if it keeps up with the reviewer. Every review left on your PRs
is training data. This is the procedure for turning it into a rule.

## Data model

Two files, one job each:

| File | Holds | Edited |
|---|---|---|
| `references/catalog.json` | The raw evidence — one anonymised entry per review comment, keyed by comment id | Append-only, via `/fran-learn` |
| `references/rules.md` | The distilled rules — detection + canonical fix | Edited when evidence sharpens a rule or a new one appears |
| `references/sources.json` | The reviewer's login and the repositories to harvest | Local only, gitignored — the one file naming anything real |

`scripts/harvest.py` compares the two by `id`. **The id is the dedup key: never invent one,
never edit one.** An entry whose `id` does not exist on GitHub makes the loop lie about what
has been seen.

## Procedure

### 1. Harvest

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/harvest.py"
```

Prints only comments absent from the catalog, each with:
- the comment body and its permalink
- the diff hunk it was anchored to (what the code looked like when he objected)
- commits pushed to that PR after the comment (the likely fix)

`--all` replays everything, `--json` for machine consumption, `--repo owner/name` to narrow.

### 2. Classify each new comment

Ask, in order:

1. **Is it a rule at all?** Some comments are project questions, not standards
   (*"is this the right catalog code?"*). Those do not belong in the catalog. Skip them.
2. **Does an existing rule already cover it?** Match on the *defect*, not the wording.
   *"use a pydantic object"*, *"apply the schema here"* and *"make the payload a pydantic
   object"* are all R1 — three phrasings, one rule.
3. **If new**, allocate the next free `R<n>`. Do not renumber existing rules: `catalog.json`
   references them by id and old entries must keep pointing at the same rule.

### 3. Write it down

**Confirming an existing rule** — append to `catalog.json`:

```json
{
  "id": "<review comment id>", "repo": "repo-a", "pr": 21, "date": "2026-09-02",
  "layer": "domain", "rule": "R1",
  "quote": "<their exact words, typos included; substitute domain identifiers only>",
  "context": "<what the code was doing>",
  "fix": "<what landed, or PENDING>",
  "fix_commit": "<sha or null>"
}
```

Bump the rule's evidence count in the `SKILL.md` table. Add the quote to `rules.md` only if
it says something the existing quotes do not.

**A new rule** — add a section to `rules.md` in the standard shape:

```markdown
## R<n> — <one-line statement of the standard> · <BLOCK|ASK> · <k> comment(s)

> *"<their exact words>"* · <repo-label>#<pr> · <layer>

### Detection
<what to look for, concretely enough that someone could grep or parse for it>

### Canonical fix
<before/after code, taken from the commit that he approved>
```

Then: add the row to the `SKILL.md` table, and add a check to `scripts/scan.py` **only if it
can be detected mechanically without noise**. A rule that needs judgement belongs in step 3
of the review workflow ("Review what the scanner cannot see"), not in the scanner. A noisy
check is worse than no check — it trains people to ignore the output.

### 4. Close the loop on the fix

When the follow-up commit lands, fill in `fix` and `fix_commit`. `"fix": "PENDING"` means the
rule is known but its canonical resolution is not — the scanner can flag it, but the skill
cannot yet tell anyone what "right" looks like.

### 5. Ship it

Update `last_harvest`, commit, open a PR on this repo. Anyone else using the plugin picks it
up by pulling their clone and re-running `install.sh`.

## Severity

- **BLOCK** — he has left CHANGES_REQUESTED over it.
- **ASK** — he questioned it without blocking, or it was resolved by explanation.

A rule can be promoted ASK → BLOCK once a review blocks over it. R8 started as four questions
(*"why two separate fields?"*) and became BLOCK once a PR was gated on it.

## Worked example — R11

**Harvest output:**

```
── NEW  repo-a#19  <domain module>:23  id=<comment id>
   COMMENT: i dont think is needed, i would say they were just examples
   CODE IT ANCHORS TO:
     +# Placeholder addresses the spec lists as rejected.
     +EMAIL_BLOCKLIST = frozenset(
```

**Classification:** not R1 (it is already a typed constant), not R5 (naming it is not the
problem — *existing* is the problem), not R8 (not a response field). The defect is that a list
of examples from a spec was implemented as an enforced rule. **New rule: R11.**

**Detection, mechanically:** a module-level uppercase constant bound to a
`frozenset`/`set`/`tuple`/`list` of string literals whose name matches
`BLOCKLIST|WHITELIST|EXAMPLES|PLACEHOLDER|SAMPLE|KNOWN_|DUMMY|FAKE`. Low enough noise to
automate → added to `scan.py` as `check_r11_spec_examples`, severity ASK because the answer
depends on what the spec said.

**Canonical fix:** unknown at the time — the PR was still open, so the entry went in with
`"fix": "PENDING"`.

**Verification:** running `scan.py` against that file at that commit reproduces the finding at
the exact line commented on. That round trip — *a comment → a rule → the scanner finds it at
that line* — is the acceptance test for any new mechanical check.

---
name: fran-best-practices
description: This skill should be used before opening or updating a pull request on a Python service, or whenever the user asks to "review before the PR", "pre-PR review", "fran review", "revisar antes de la PR", or to fold new reviewer feedback into the ruleset. It reviews a diff against twelve rules distilled from real code reviews — Pydantic modelling, error-flow discipline, exception design, enums over dicts, magic values, code language, encapsulation by protocol, redundant fields, backward compatibility, comment quality, spec examples, transport boundaries — and can harvest a reviewer's new comments to grow the ruleset.
---

# Pre-PR review

Twelve rules distilled from 24 real review comments across 7 pull requests that were blocked
and then approved. The point is to land the finding before a reviewer has to write it.

Two modes:

- **Review** (default) — check a diff before pushing. See *Review workflow*.
- **Learn** — harvest a reviewer's new comments and grow the ruleset. See *Feedback loop*.

## The 12 rules

| # | Rule | Sev | Evidence |
|---|---|---|---|
| **R1** | Model everything. No `dict[str, Any]` pass-through — request, response, domain **and mocks**. | BLOCK | 6× |
| **R2** | Errors are business logic. The caller always gets **200**; client raises → service translates to a result code. | BLOCK | 1× |
| **R3** | Exceptions: one subclass per root cause, safe log message, raise instead of returning a sentinel. | BLOCK | inferred |
| **R4** | `Enum` instead of `dict` for a closed set (no bare `KeyError`). | BLOCK | 1× |
| **R5** | No magic strings or numbers — named constant or setting, with provenance. | BLOCK | 3× |
| **R6** | Code is written in English; the product's language only in customer-facing fields. | BLOCK | 3× |
| **R7** | Encapsulate by protocol: one client per upstream, backend fixed at construction. | BLOCK | 2× |
| **R8** | No redundant fields; declare each once; required by default. | BLOCK | 4× |
| **R9** | No backward compatibility before production. | BLOCK | 1× |
| **R10** | Comments explain *why* and define what they cite. | ASK | 1× |
| **R11** | Examples in a spec are not business rules. | ASK | 1× |
| **R12** | Mind the transport/model boundary. | ASK | 1× |

Full detail — the reviewer's literal words, detection guidance and the canonical fix for each
— is in `references/rules.md`. **Read it before reporting findings**; the table above is only
an index. Raw evidence is `references/catalog.json`.

## Review workflow

### 1. Establish the diff

```bash
git rev-parse --abbrev-ref HEAD
git remote show origin | sed -n 's/.*HEAD branch: //p'    # the real base branch
git merge-base HEAD origin/<base>
git diff --stat $(git merge-base HEAD origin/<base>) HEAD
```

Do not assume `main` — some repos target `develop`. If the user names a PR, use
`gh pr diff <n>`. If they name paths, use those. If the working tree is dirty, include the
uncommitted changes and say so.

### 2. Run the deterministic scan

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/scan.py" --diff origin/<base>
```

`scan.py` parses with `ast`, so a finding points at a real construct, not a substring. It
covers R1, R2, R3, R4, R5, R6, R8, R9, R11, R12 mechanically. Add `--json` to post-process,
`--rules R1,R6` to narrow. Exit code 1 means at least one BLOCK finding.

Layer-dependent rules use a conventional layout (`app/domain`, `app/services`,
`app/api/schemas`, `app/api/routes`, `app/clients`). A repo laid out differently declares its
own in a `.fran-scan.json` at the root — see the README. The layer-independent rules work
anywhere with no configuration.

Treat the output as **candidates, not verdicts**. Verify each against the real code, and drop
it if the context justifies it. Known judgement calls:

- **R8 all-optional models**: response envelopes whose branches fill different fields are
  optional by design and are already skipped. Only report this for models the diff introduces.
- **R5 repeated literals**: dict keys are excluded, but a literal repeated across genuinely
  unrelated call sites may be fine. A backend name never is — that is R7.
- **R3 sentinel returns**: legitimate inside a service that is translating to a result code.
- **R11**: always a question, never an assertion. Ask what the spec actually said.

### 3. Review what the scanner cannot see

These need reading the diff, not parsing it:

- **R2 layering** — trace each new failure path. Does an exception escape a service? Does every
  failure mode have its own result code with an actionable next step? Does a best-effort
  dependency degrade rather than escalate? **Is there a test for the failure path?** Compare
  against the neighbouring endpoint — the review said *"See other endpoints for reference"*.
- **R7 placement** — is upstream-specific logic (encoding, date formats, envelope unwrapping)
  sitting in the domain layer? It belongs in the client.
- **R8 justification** — for each new field, can you say in one sentence why it exists and why
  the caller cannot derive it? If not, expect the question.
- **R10 comments** — list the acronyms, product names and standards the diff introduces. Any a
  new teammate could not look up needs a sentence. Never suggest *shortening* a comment.
- **Tests** — did behaviour change without the tests changing shape? Model refactors change
  assertions from dict comparison to model comparison.

### 4. Report

Group by rule, BLOCK before ASK. For each finding give `file:line`, what is wrong, the concrete
fix, and the quote that backs it. Close with:

> **N blocking, M to justify.** This would be blocked / would pass.

Then offer to apply the fixes.

## How to deliver the fix

Measured across the 7 pull requests that were blocked:

- **Fix it in the whole file, not just the flagged line.** A `"same"` from a reviewer means
  "apply this everywhere here". One comment flagged one constant; the accepted fix extracted six.
- **Separate, labelled commit** — `refactor(review): ...`. Never amend the original: the
  reviewer wants to see the delta.
- **Update the tests in the same commit.**
- **Reply in the thread when the change is conceptual.** The one substantive reply in the
  history — what moved, where, and that a test was added — got an approval in 27 minutes.

## Anti-patterns attributed to Claude

Put these in the prompt before generating code:

1. **Unrequested backward compatibility.** Called out verbatim as *"a very common claude thing,
   tell him to not care about it."* Delete, do not adapt.
2. `dict[str, Any]` return types in domain and mocks because it is quicker.
3. Defensive all-optional models (`x: T | None = None` on every field).
4. The same data in two shapes (structured + a pre-rendered string).
5. Hand-rolled validation the framework already does.

## Feedback loop

The ruleset is meant to grow. After a reviewer goes through a PR:

```bash
SRC=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/source_root.py")   # the git checkout to edit
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/harvest.py"
```

**Write to `$SRC`, never to `${CLAUDE_PLUGIN_ROOT}`.** The plugin runs from
`~/.claude/plugins/cache/...`, a copy replaced on every reinstall; edits there are lost.
`source_root.py` resolves the real checkout and exits 3 with instructions when there is
nowhere safe to write.

`harvest.py` needs `~/.claude/fran-best-practices/sources.json` — the local file naming the
reviewer and the repositories. It lives outside the plugin so it survives updates, and it is
the only file that identifies anyone, which is what keeps the published ruleset generic. If it
is missing, `harvest.py` prints the exact path and the example to copy.

For each new comment:

1. **Classify** — does it confirm an existing rule, or is it a new one?
2. **Confirms a rule** → add the entry to `catalog.json` with that `rule`, and add the quote to
   that rule's evidence in `rules.md` if it sharpens it.
3. **New rule** → allocate the next `R<n>`, write it in `rules.md` in the standard shape
   (evidence · severity · detection · canonical fix), add it to the table above, and add a
   check to `scripts/scan.py` **only if it can be detected mechanically without noise**. A rule
   that needs judgement lives in step 3 of the review workflow instead.
4. **Anonymise as you go** — no real repository names, file paths, people, or client systems
   in `catalog.json` or `rules.md`. Repos are opaque labels, paths are reduced to the layer.
5. **Record the fix** once the follow-up commit lands. `"fix": "PENDING"` means the rule is
   known but its canonical resolution is not.
6. Bump `last_harvest`, then commit and open a PR so anyone else using this gets it.

Never invent or edit a comment `id` — it is the dedup key. Full procedure and a worked example:
`references/feedback-loop.md`.

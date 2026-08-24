# fran-best-practices

A Claude Code plugin that reviews your diff before a reviewer has to.

Twelve rules for Python services, distilled from **24 real pull-request review comments across
7 PRs** that were blocked and then approved — each traced to the fix that was actually
accepted. Not what a generic linter thinks: what a demanding reviewer actually blocks on.

The evidence is anonymised. Repositories are opaque labels, file paths are reduced to the
architectural layer, and domain identifiers inside quotes are substituted for neutral
equivalents — the objection each quote makes is unchanged.

## Install

```bash
gh repo clone <owner>/fran-best-practices ~/.claude/fran-best-practices-src \
  && ~/.claude/fran-best-practices-src/install.sh
```

Restart Claude Code. Needs `python3`, plus `gh` authenticated if you want the feedback loop.

If the repository is private, clone with `gh` rather than plain `git`: `gh` uses its own token,
while `claude plugin marketplace add <owner>/<repo>` cannot read a private repo at all
(verified — it fails even with `GH_TOKEN` set).

**Updating** is pull-based; a push does not reach anyone automatically:

```bash
git -C ~/.claude/fran-best-practices-src pull && ~/.claude/fran-best-practices-src/install.sh
```

## Use

```
/fran-review              # this branch vs its base
/fran-review 21           # PR #21
/fran-review src/domain   # specific paths
/fran-learn               # fold a reviewer's new comments into the ruleset
```

The skill also triggers on its own when you ask for a pre-PR review.

The scanner runs standalone, so it drops into a pre-commit hook or CI:

```bash
python3 ~/.claude/plugins/cache/*/fran-best-practices/*/scripts/scan.py --diff origin/main
# exit 1 when there is at least one blocking finding
```

## The rules

| # | Rule | Sev | Seen |
|---|---|---|---|
| R1 | Model everything. No `dict[str, Any]` pass-through — including mocks. | BLOCK | 6× |
| R2 | Errors are business logic. The caller always gets 200. | BLOCK | 1× |
| R3 | Exceptions: one per root cause, safe log message, never a sentinel. | BLOCK | — |
| R4 | `Enum` instead of `dict` for a closed set. | BLOCK | 1× |
| R5 | No magic strings or numbers. | BLOCK | 3× |
| R6 | Code is written in English. | BLOCK | 3× |
| R7 | One client per upstream; backend fixed at construction. | BLOCK | 2× |
| R8 | No redundant fields; declare once; required by default. | BLOCK | 4× |
| R9 | No backward compatibility before production. | BLOCK | 1× |
| R10 | Comments explain *why* and define what they cite. | ASK | 1× |
| R11 | Examples in a spec are not business rules. | ASK | 1× |
| R12 | Mind the transport/model boundary. | ASK | 1× |

Full detail in
[`rules.md`](plugins/fran-best-practices/skills/fran-best-practices/references/rules.md).

## How the detection works

`scripts/scan.py` parses Python with `ast` and `tokenize` rather than grepping, so a finding
points at a real construct. It was validated by replay: checking out the commit each review was
written against and confirming the scanner reproduces the finding at the same line.

| Review | What it caught |
|---|---|
| Duplicate fields, loose return types, a compat shim | 6/6, exact lines |
| Three non-English comments, a hardcoded backend name | all, exact lines |
| A closed-set dict subscript that could `KeyError` | yes |
| A spec's examples hardcoded as a blocklist | yes, exact line |

Rules that need judgement — R2 layering, R7 placement, R10 comment quality — are reviewed by
the model against `rules.md`. A noisy automated check is worse than none: it trains people to
ignore the output.

## Using it on another repo

**Layer conventions.** The layout-dependent rules (R1, R2, R7) default to a conventional
FastAPI service. A repo laid out differently declares its own in a `.fran-scan.json` at the
root:

```json
{
  "layers": {
    "domain":   ["src/domain"],
    "services": ["src/services"],
    "schemas":  ["src/schemas"],
    "routes":   ["src/api"],
    "clients":  ["src/clients"]
  }
}
```

The layer-independent rules — R6 language, R9 backward compatibility, R3 exception handling —
apply everywhere with no configuration, so it is useful in a new repo on day one.

**Whose comments to learn from.** Copy
`plugins/fran-best-practices/skills/fran-best-practices/references/sources.example.json` to
`sources.json` (gitignored) and fill in the reviewer's login and the repositories:

```json
{ "reviewer": "<github-login>", "repos": ["<owner>/<repo>"] }
```

That is the only file naming anything real, which is what keeps the ruleset itself generic and
shareable.

## Feedback loop

The ruleset grows. After a reviewer goes through a PR, `/fran-learn` diffs GitHub against the
evidence catalog by comment id and surfaces only what is new — with the diff hunk it anchored
to and the commits pushed after it, so a rule can be written from the fix that was actually
approved.

Edits land in the git checkout, never in the plugin cache (`source_root.py` resolves it, and
refuses to guess). New rules land as a commit here.

See
[`feedback-loop.md`](plugins/fran-best-practices/skills/fran-best-practices/references/feedback-loop.md).

## Layout

```
.claude-plugin/marketplace.json
plugins/fran-best-practices/
├── .claude-plugin/plugin.json
├── commands/{fran-review,fran-learn}.md
├── scripts/{scan.py,harvest.py,source_root.py}
└── skills/fran-best-practices/
    ├── SKILL.md
    └── references/{rules.md,catalog.json,feedback-loop.md,sources.example.json}
```

---

By Marc Aguilar.

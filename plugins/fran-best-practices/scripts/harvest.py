#!/usr/bin/env python3
"""Harvest new review comments by the reviewer and report the ones the ruleset has not seen.

This is the feedback loop behind ``/fran-learn``. It reads the known comment ids out of
``references/catalog.json`` and the reviewer/repositories out of the local, gitignored
``references/sources.json``, asks the GitHub API for every review comment and review body the
reviewer has left on those repos, and prints the ones that are new — each with the
diff hunk it was anchored to and the commits that landed after it, which is the evidence
needed to decide whether it confirms an existing rule or is a new one.

It never writes to the catalog. Promoting a finding into a rule is a judgement call the model
makes with the user, and lands as a reviewed commit to this repo so the whole team gets it.

Requires the `gh` CLI, authenticated.

Usage:
    harvest.py                       # new comments across all configured repos
    harvest.py --repo owner/name     # restrict to one repo (repeatable)
    harvest.py --all                 # every comment, not just the unseen ones
    harvest.py --json                # machine-readable
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_RELATIVE_REFERENCES = Path("plugins") / "fran-best-practices" / "skills" / "fran-best-practices" / "references"
_LOCAL_REFERENCES = Path(__file__).resolve().parent.parent / "skills" / "fran-best-practices" / "references"
CATALOG = _LOCAL_REFERENCES / "catalog.json"
SOURCES_EXAMPLE = _LOCAL_REFERENCES / "sources.example.json"


def sources_path() -> Path:
    """Where sources.json lives: the git checkout first, this plugin copy second.

    The plugin may be running from ~/.claude/plugins/cache/..., which is replaced on every
    reinstall. Telling someone to create their config there would lose it on the next update,
    so the checkout wins whenever one can be found.
    """
    try:
        import source_root  # same directory; Python puts it on sys.path
        checkout, _how, _tried = source_root.resolve(fetch=False)
    except Exception:
        checkout = None
    if checkout is not None:
        candidate = checkout / _RELATIVE_REFERENCES / "sources.json"
        if candidate.exists():
            return candidate
        return candidate  # report this path in the error, so the file is created where it lasts
    return _LOCAL_REFERENCES / "sources.json"


def gh_api(path: str, jq: str | None = None) -> object:
    cmd = ["gh", "api", path, "--paginate"]
    if jq:
        cmd += ["--jq", jq]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"harvest: gh api {path} failed: {result.stderr.strip()}", file=sys.stderr)
        return []
    text = result.stdout.strip()
    if not text:
        return []
    if jq:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    # --paginate concatenates JSON arrays; parse defensively.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        out: list[object] = []
        decoder = json.JSONDecoder()
        index = 0
        while index < len(text):
            while index < len(text) and text[index] in " \n\r\t":
                index += 1
            if index >= len(text):
                break
            value, index = decoder.raw_decode(text, index)
            out.extend(value) if isinstance(value, list) else out.append(value)
        return out


def load_catalog() -> dict:
    if not CATALOG.exists():
        print(f"harvest: catalog not found at {CATALOG}", file=sys.stderr)
        sys.exit(2)
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def load_sources() -> dict:
    """Read the local, gitignored config naming the reviewer and the repositories.

    Kept out of the published plugin on purpose: it is the only file that identifies real
    people and repositories, so the ruleset itself stays generic and shareable.
    """
    sources = sources_path()
    if not sources.exists():
        print(f"harvest: sources.json not found.\n"
              f"  cp {SOURCES_EXAMPLE} {sources}\n"
              f"  then fill in the reviewer's GitHub login and the repositories to harvest.\n"
              f"  (it is gitignored: it is the only file that names real repos and people)",
              file=sys.stderr)
        sys.exit(2)
    data = json.loads(sources.read_text(encoding="utf-8"))
    reviewer, repos = data.get("reviewer"), data.get("repos")
    if not reviewer or not isinstance(repos, list) or not repos:
        print(f"harvest: {sources} needs a 'reviewer' login and a non-empty 'repos' list.",
              file=sys.stderr)
        sys.exit(2)
    return data


def pull_numbers(repo: str) -> list[int]:
    rows = gh_api(f"repos/{repo}/pulls?state=all&per_page=100", jq=".[].number")
    return [int(n) for n in rows]


def harvest_repo(repo: str, reviewer: str) -> list[dict]:
    found: list[dict] = []
    for number in pull_numbers(repo):
        inline = gh_api(
            f"repos/{repo}/pulls/{number}/comments",
            jq=f'.[] | select(.user.login=="{reviewer}") | '
               "{id: (.id|tostring), path: .path, line: (.line // .original_line), "
               "body: .body, diff_hunk: .diff_hunk, created_at: .created_at, url: .html_url}",
        )
        for row in inline:
            row.update(repo=repo.split("/")[-1], pr=number, kind="inline")
            found.append(row)
        reviews = gh_api(
            f"repos/{repo}/pulls/{number}/reviews",
            jq=f'.[] | select(.user.login=="{reviewer}" and (.body|length>0)) | '
               '{id: ("review-" + (.id|tostring)), path: "<review-body>", line: 0, '
               "body: .body, diff_hunk: \"\", created_at: .submitted_at, url: .html_url}",
        )
        for row in reviews:
            row.update(repo=repo.split("/")[-1], pr=number, kind="review-body")
            found.append(row)
    return found


def commits_after(repo: str, number: int, after: str) -> list[str]:
    """Commit subjects pushed after the comment — the likely fix."""
    rows = gh_api(
        f"repos/{repo}/pulls/{number}/commits",
        jq='.[] | {date: .commit.committer.date, msg: (.commit.message | split("\\n")[0])}',
    )
    return [f"{r['date']}  {r['msg']}" for r in rows if r["date"] > after]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", action="append", dest="repos", help="owner/name (repeatable)")
    parser.add_argument("--all", action="store_true", help="include comments already in the catalog")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()

    catalog = load_catalog()
    sources = load_sources()
    reviewer = sources["reviewer"]
    repos = args.repos or sources["repos"]
    known = {c["id"] for c in catalog["comments"]}
    known_rules = sorted({c["rule"] for c in catalog["comments"]})

    harvested: list[dict] = []
    for repo in repos:
        harvested.extend(harvest_repo(repo, reviewer))
    harvested.sort(key=lambda c: c["created_at"])

    new = harvested if args.all else [c for c in harvested if c["id"] not in known]
    for comment in new:
        full = next(r for r in repos if r.endswith(comment["repo"]))
        comment["commits_after"] = commits_after(full, comment["pr"], comment["created_at"])

    if args.json:
        print(json.dumps({
            "reviewer": reviewer,
            "repos": repos,
            "known_rules": known_rules,
            "catalog_entries": len(known),
            "harvested": len(harvested),
            "new": new,
        }, indent=2, ensure_ascii=False))
        return 0

    print(f"harvest: reviewer={reviewer}  repos={', '.join(repos)}")
    print(f"         catalog has {len(known)} comment(s) across rules {', '.join(known_rules)}")
    print(f"         GitHub has {len(harvested)} comment(s); {len(new)} not in the catalog\n")
    if not new:
        print("Nothing new. The ruleset is up to date.")
        return 0
    for comment in new:
        print(f"── NEW  {comment['repo']}#{comment['pr']}  {comment['path']}:{comment['line']}  "
              f"({comment['created_at'][:10]})  id={comment['id']}")
        print(f"   {comment['url']}")
        print(f"   COMMENT: {comment['body'].strip()}")
        if comment["diff_hunk"]:
            hunk = comment["diff_hunk"].splitlines()
            tail = hunk[-14:] if len(hunk) > 14 else hunk
            print("   CODE IT ANCHORS TO:")
            for line in tail:
                print(f"     {line}")
        if comment["commits_after"]:
            print("   COMMITS AFTER (the likely fix):")
            for line in comment["commits_after"]:
                print(f"     {line}")
        print()
    print(f"{len(new)} comment(s) to classify. Existing rules: {', '.join(known_rules)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

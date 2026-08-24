#!/usr/bin/env python3
"""Locate the plugin's SOURCE checkout — the git clone edits must land in.

Why this exists: a plugin runs from ``~/.claude/plugins/cache/<marketplace>/<name>/<version>/``,
which is a throwaway copy replaced on every reinstall. ``/fran-learn`` grows the ruleset, so its
edits have to reach the real repository or they vanish and teammates never see them.

Resolution order:

1. ``$FRAN_BP_SOURCE``, if it points at a checkout.
2. The marketplace registration in Claude settings. A ``directory`` source IS the checkout
   (the local-development case).
3. A ``github`` source: clone it under ``~/.claude/fran-best-practices-src`` (or pull if already
   there) and use that. This is the case for everyone who installed from GitHub.

Prints the absolute path on success. Exits 3 with an explanation when there is nowhere to write,
so the caller can tell the user what to do instead of silently editing the cache.

Usage:
    source_root.py             # print the path, cloning/pulling if needed
    source_root.py --no-fetch  # never touch the network
    source_root.py --json      # path plus how it was resolved
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

MARKETPLACE = "marcaguilar"
PLUGIN = "fran-best-practices"
FALLBACK_CLONE = Path.home() / ".claude" / f"{PLUGIN}-src"
SETTINGS = [
    Path.home() / ".claude" / "settings.json",
    Path.home() / ".claude" / "settings.local.json",
]
MARKER = Path(".claude-plugin") / "marketplace.json"


def is_checkout(path: Path) -> bool:
    return (path / MARKER).is_file()


def git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def registered_source() -> dict | None:
    for settings_path in SETTINGS:
        if not settings_path.is_file():
            continue
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        entry = (data.get("extraKnownMarketplaces") or {}).get(MARKETPLACE)
        if isinstance(entry, dict) and isinstance(entry.get("source"), dict):
            return entry["source"]
    return None


def ensure_clone(repo: str, *, fetch: bool) -> tuple[Path | None, str]:
    url = repo if repo.startswith(("http", "git@")) else f"https://github.com/{repo}.git"
    if is_checkout(FALLBACK_CLONE):
        if fetch:
            git("pull", "--ff-only", "--quiet", cwd=FALLBACK_CLONE)
        return FALLBACK_CLONE, f"clone of {repo}"
    if not fetch:
        return None, f"{FALLBACK_CLONE} is not a checkout and --no-fetch was given"
    FALLBACK_CLONE.parent.mkdir(parents=True, exist_ok=True)
    result = git("clone", "--quiet", url, str(FALLBACK_CLONE))
    if result.returncode != 0 or not is_checkout(FALLBACK_CLONE):
        return None, f"could not clone {url}: {result.stderr.strip()}"
    return FALLBACK_CLONE, f"fresh clone of {repo}"


def resolve(*, fetch: bool) -> tuple[Path | None, str, list[str]]:
    tried: list[str] = []

    override = os.environ.get("FRAN_BP_SOURCE")
    if override:
        candidate = Path(override).expanduser().resolve()
        if is_checkout(candidate):
            return candidate, "$FRAN_BP_SOURCE", tried
        tried.append(f"$FRAN_BP_SOURCE={override} is not a checkout (no {MARKER})")

    source = registered_source()
    if source is None:
        tried.append(f"no '{MARKETPLACE}' marketplace found in {' or '.join(str(p) for p in SETTINGS)}")
    elif source.get("source") == "directory":
        candidate = Path(source.get("path", "")).expanduser().resolve()
        if is_checkout(candidate):
            return candidate, "marketplace registered as a local directory", tried
        tried.append(f"registered directory {candidate} is not a checkout")
    elif source.get("source") in {"github", "git"}:
        repo = source.get("repo") or source.get("url") or ""
        if repo:
            path, how = ensure_clone(repo, fetch=fetch)
            if path is not None:
                return path, how, tried
            tried.append(how)
        else:
            tried.append("marketplace github source has neither 'repo' nor 'url'")
    else:
        tried.append(f"unsupported marketplace source type {source.get('source')!r}")

    return None, "", tried


def describe(path: Path) -> dict:
    branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=path).stdout.strip()
    remote = git("remote", "get-url", "origin", cwd=path).stdout.strip()
    dirty = bool(git("status", "--porcelain", cwd=path).stdout.strip())
    return {"branch": branch or None, "remote": remote or None, "dirty": dirty}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-fetch", action="store_true", help="never clone or pull")
    parser.add_argument("--json", action="store_true", help="also report how it was resolved")
    args = parser.parse_args()

    path, how, tried = resolve(fetch=not args.no_fetch)
    if path is None:
        print("source_root: cannot find the plugin's source checkout.", file=sys.stderr)
        for line in tried:
            print(f"  - {line}", file=sys.stderr)
        print("\nFix it with either:", file=sys.stderr)
        print("  export FRAN_BP_SOURCE=/path/to/fran-best-practices", file=sys.stderr)
        print(f"  git clone <your fork of this plugin> {FALLBACK_CLONE}", file=sys.stderr)
        return 3

    if args.json:
        print(json.dumps({"path": str(path), "resolved_via": how, **describe(path)}, indent=2))
    else:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

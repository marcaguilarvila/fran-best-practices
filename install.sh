#!/usr/bin/env bash
# Install the fran-best-practices plugin for Claude Code.
#
#   gh repo clone marcaguilarvila/fran-best-practices ~/.claude/fran-best-practices-src \
#     && ~/.claude/fran-best-practices-src/install.sh
#
# or, from a clone you already have:   ./install.sh
#
# The repository is private, so this deliberately does NOT offer a `curl | bash` one-liner:
# raw.githubusercontent.com will not serve it, and `claude plugin marketplace add <owner/repo>`
# cannot authenticate against a private repo either (verified — it fails even with GH_TOKEN set).
# Cloning with `gh` uses your gh token, which is why that is the supported path.
set -euo pipefail

REPO="${FRAN_BP_REPO:-marcaguilarvila/fran-best-practices}"
MARKETPLACE="marcaguilar"
PLUGIN="fran-best-practices"
DEFAULT_CLONE="$HOME/.claude/${PLUGIN}-src"

die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarn:\033[0m %s\n' "$*"; }

command -v claude  >/dev/null 2>&1 || die "Claude Code CLI not found. See https://claude.com/claude-code"
command -v python3 >/dev/null 2>&1 || die "python3 not found (the scanner needs it)."
command -v git     >/dev/null 2>&1 || die "git not found."

# Run from a clone? Use it. Otherwise fetch one with gh, which is the only thing that can read
# a private repo without extra credential setup.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/.claude-plugin/marketplace.json" ]; then
  SOURCE="$SELF_DIR"
  info "Installing from this clone: $SOURCE"
elif [ -f "$DEFAULT_CLONE/.claude-plugin/marketplace.json" ]; then
  SOURCE="$DEFAULT_CLONE"
  info "Updating the existing clone at $SOURCE"
  git -C "$SOURCE" pull --ff-only --quiet || warn "could not pull; installing what is on disk"
else
  command -v gh >/dev/null 2>&1 || die "gh CLI not found, and no clone at $DEFAULT_CLONE.
  Install gh (brew install gh && gh auth login), or clone the repo yourself and run ./install.sh from it."
  info "Cloning $REPO into $DEFAULT_CLONE..."
  mkdir -p "$(dirname "$DEFAULT_CLONE")"
  gh repo clone "$REPO" "$DEFAULT_CLONE" -- --quiet \
    || die "clone failed. Do you have access to $REPO? Ask Marc to add you as a collaborator."
  SOURCE="$DEFAULT_CLONE"
fi

command -v gh >/dev/null 2>&1 || warn "gh not found — /fran-learn needs it to read review comments."

info "Registering the marketplace..."
claude plugin marketplace add "$SOURCE" 2>/dev/null || claude plugin marketplace update "$MARKETPLACE"

info "Installing the plugin..."
claude plugin install "${PLUGIN}@${MARKETPLACE}" --scope user --yes

cat <<DONE

  Installed from $SOURCE

  Restart Claude Code, then:

    /fran-review          review this branch before you push
    /fran-review 21       review PR #21
    /fran-learn           fold Fran's new comments into the ruleset

  The skill also loads on its own when you ask for a pre-PR review.

  For /fran-learn, create references/sources.json from the example first:
    it names the reviewer and the repos to harvest, and is gitignored.

  To pick up new rules later:
    git -C $SOURCE pull && $SOURCE/install.sh

DONE

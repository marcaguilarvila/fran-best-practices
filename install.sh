#!/usr/bin/env bash
# Install the fran-best-practices plugin for Claude Code.
#
#   curl -fsSL https://raw.githubusercontent.com/marcaguilarvila/fran-best-practices/main/install.sh | bash
#
# Run from a clone instead, and the clone itself is registered, so /fran-learn writes the rules
# it learns straight into your checkout:
#
#   ./install.sh
set -euo pipefail

REPO="${FRAN_BP_REPO:-marcaguilarvila/fran-best-practices}"
MARKETPLACE="marcaguilar"
PLUGIN="fran-best-practices"
CLONE="$HOME/.claude/${PLUGIN}-src"
MARKER=".claude-plugin/marketplace.json"

die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarn:\033[0m %s\n' "$*"; }

command -v claude  >/dev/null 2>&1 || die "Claude Code not found. See https://claude.com/claude-code"
command -v python3 >/dev/null 2>&1 || die "python3 not found (the scanner needs it)."

# Prefer a checkout when there is one: a directory marketplace lets /fran-learn write the rules
# it learns into a real repository instead of the throwaway plugin cache. Otherwise install
# straight from GitHub, which needs no git, no gh and no credentials.
SELF_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ "${BASH_SOURCE[0]}" != "bash" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/$MARKER" ]; then
  SOURCE="$SELF_DIR"; MODE="clone"
  info "Installing from this checkout: $SOURCE"
elif [ -f "$CLONE/$MARKER" ]; then
  SOURCE="$CLONE"; MODE="clone"
  info "Using the existing checkout at $SOURCE"
  git -C "$SOURCE" pull --ff-only --quiet 2>/dev/null || warn "could not pull; installing what is on disk"
else
  SOURCE="$REPO"; MODE="github"
  info "Installing from GitHub: $SOURCE"
fi

info "Registering the marketplace..."
claude plugin marketplace add "$SOURCE" >/dev/null 2>&1 \
  || claude plugin marketplace update "$MARKETPLACE" >/dev/null 2>&1 \
  || die "could not register the marketplace from $SOURCE"

info "Installing the plugin..."
claude plugin install "${PLUGIN}@${MARKETPLACE}" --scope user --yes

command -v gh >/dev/null 2>&1 || warn "gh not found — /fran-learn needs it to read review comments."

cat <<DONE

  Installed. Restart Claude Code, then, from any repo you want reviewed:

    /fran-review          review this branch before you push
    /fran-review 21       review PR #21
    /fran-learn           fold a reviewer's new comments into the ruleset

  The skill also loads on its own when you ask for a pre-PR review.

  For /fran-learn, create ~/.claude/fran-best-practices/sources.json with the reviewer's
  GitHub login and the repos to harvest. It will tell you the exact path if it is missing.
DONE

if [ "$MODE" = "github" ]; then
  cat <<DONE
  Update later with:
    claude plugin marketplace update $MARKETPLACE && claude plugin update $PLUGIN

DONE
else
  cat <<DONE
  This is a directory marketplace, which 'claude plugin update' does not refresh. After
  editing the rules, reinstall:
    claude plugin uninstall $PLUGIN && claude plugin install ${PLUGIN}@${MARKETPLACE} --yes

DONE
fi

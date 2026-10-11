#!/usr/bin/env bash
# Release guard: classify the state of the release for $1 before any upload.
# Prints exactly one state on stdout:
#   none   - no release exists for the tag (GitHub answered 404)
#   draft  - an existing draft release may be clobbered (--clobber required)
# Exits nonzero for a public release, unknown args, or any gh api failure
# other than 404 (a metadata query error must never read as "absent").
# Env: GITHUB_REPOSITORY, GH_TOKEN (as gh requires).
set -euo pipefail

if [ "$#" -eq 0 ] || [ -z "${1:-}" ]; then
  echo "usage: check_release_state.sh <tag> [--clobber]" >&2
  exit 2
fi
tag="$1"
shift
clobber=false
for arg in "$@"; do
  if [ "$arg" = "--clobber" ]; then
    clobber=true
  else
    echo "check_release_state: unknown argument $arg" >&2
    exit 2
  fi
done

repo="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY must be set}"
draft_file="$(mktemp)"
err_file="$(mktemp)"
trap 'rm -f "$draft_file" "$err_file"' EXIT

if gh api "repos/$repo/releases/tags/$tag" --jq .draft >"$draft_file" 2>"$err_file"; then
  draft="$(cat "$draft_file")"
  if [ "$draft" = "true" ]; then
    if $clobber; then
      echo "draft"
      exit 0
    fi
    echo "check_release_state: release $tag exists as a draft; rerun with --clobber to re-upload" >&2
    exit 1
  fi
  echo "check_release_state: release $tag is public; refusing to modify or replace it" >&2
  exit 1
fi

if grep -q "HTTP 404" "$err_file"; then
  echo "none"
  exit 0
fi
echo "check_release_state: release metadata query failed for $tag: $(cat "$err_file")" >&2
exit 1

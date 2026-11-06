#!/usr/bin/env bash
# flake0-collect: redact secrets from collected log files, in place. stop.sh runs
# it after the watchers have stopped and the files are owned by the runner user,
# but before the bundle is built, so no secret a workload logged reaches the
# uploaded artifact. Two layers:
#   1. exact matches of the newline-separated values in $FLAKE0_REDACT_VALUES,
#      e.g. the job's known credentials -- the reliable layer.
#   2. the best-effort patterns in redact.sed -- common formats only.
# It is a no-op with no inputs or no paths, because redaction must never fail the
# job. Files are unredacted on disk during the run and only masked here, so treat
# the runner itself as trusted; the exposure this closes is the shared artifact.
#
# Needs GNU sed (-i, -E, the I flag, the c\ command), as shipped on the runners.
set -uo pipefail

# No paths means nothing to do. Guard first: `find` with no path argument walks
# the current directory, which would rewrite unrelated files.
[ "$#" -gt 0 ] || exit 0

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SED_FILE="${FLAKE0_REDACT_SED:-$DIR/redact.sed}"
VALUES="${FLAKE0_REDACT_VALUES:-}"

# Build the sed program: one literal substitution per secret value, then the
# committed patterns. Values go first, so a pattern cannot rewrite part of a listed
# secret, and longest first, so a shorter value cannot split a longer one that
# contains it, like a password holding the username. Each value is escaped so regex metacharacters and the "/" delimiter are
# matched literally under sed -E. It goes in a file, because a long value list would
# pass the limit on one argument and sed would never run.
PROG="$(mktemp)" || exit 0
trap 'rm -f "$PROG"' EXIT
if [ -n "$VALUES" ] && [ -r "$VALUES" ]; then
  awk '{sub(/\r$/, ""); print length($0), $0}' < "$VALUES" | sort -rn | cut -d' ' -f2- \
    | while IFS= read -r v; do
      [ -n "$v" ] || continue
      echo "s/$(printf '%s' "$v" | sed 's#[][\\/.*^$&+?(){}|]#\\&#g')/***REDACTED***/g"
    done >> "$PROG"
fi
[ -r "$SED_FILE" ] && cat "$SED_FILE" >> "$PROG"
[ -s "$PROG" ] || exit 0

# Only regular files under the given paths. xargs -r skips sed when there are
# none. Failures are swallowed: a log that cannot be rewritten is left as is
# rather than failing the job, and is no worse than before redaction existed.
find "$@" -type f -print0 2> /dev/null \
  | xargs -0 -r sed -i -E -f "$PROG" 2> /dev/null || true
exit 0

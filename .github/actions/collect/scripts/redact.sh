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

# Build the sed program: committed patterns first, then one literal substitution
# per secret value. Each value is escaped so regex metacharacters and the "/"
# delimiter are matched literally.
prog=()
[ -r "$SED_FILE" ] && prog+=(-f "$SED_FILE")
if [ -n "$VALUES" ] && [ -r "$VALUES" ]; then
  while IFS= read -r v || [ -n "$v" ]; do
    [ -n "$v" ] || continue
    prog+=(-e "s/$(printf '%s' "$v" | sed 's#[][\\/.*^$&]#\\&#g')/***REDACTED***/g")
  done < "$VALUES"
fi
[ ${#prog[@]} -gt 0 ] || exit 0

# Only regular files under the given paths. xargs -r skips sed when there are
# none. Failures are swallowed: a log that cannot be rewritten is left as is
# rather than failing the job, and is no worse than before redaction existed.
find "$@" -type f -print0 2> /dev/null \
  | xargs -0 -r sed -i -E "${prog[@]}" 2> /dev/null || true
exit 0

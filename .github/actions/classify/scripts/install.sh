#!/usr/bin/env bash
# flake0-classify: install the pinned opencode into a fresh directory and print
# its path as the `bin` output. The tarball may come from the cache, so its
# SHA256 is checked every time.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

VERSION=2.2.7
case "$(uname -m)" in
  x86_64) NAME=opencode-linux-x64.tar.gz SHA256=89a6cee227b54bc011179c528a5e63176032462b1a5e21723b616801cddc2ca7 ;;
  aarch64) NAME=opencode-linux-arm64.tar.gz SHA256=c1682057f0815cb2abdd6e00305f1146ebaf1c167f0335cdb74ddc80008120e7 ;;
  *) echo "flake0-classify: no opencode build for $(uname -m)" >&2; exit 1 ;;
esac

TARBALL="${TARBALL_DIR:?}/$NAME"
mkdir -p "$TARBALL_DIR"
if ! echo "$SHA256  $TARBALL" | sha256sum -c - > /dev/null 2>&1; then
  curl -fsSL --retry 3 --retry-delay 5 -o "$TARBALL" \
    "https://github.com/anomalyco/opencode/releases/download/v$VERSION/$NAME"
  echo "$SHA256  $TARBALL" | sha256sum -c - > /dev/null
fi
BIN_DIR="$(mktemp -d "${RUNNER_TEMP:-/tmp}/flake0-opencode.XXXXXX")"
tar -xzf "$TARBALL" -C "$BIN_DIR" opencode
echo "bin=$BIN_DIR/opencode" >> "${GITHUB_OUTPUT:-/dev/null}"

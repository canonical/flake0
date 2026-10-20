#!/usr/bin/env bash
# A flaky-by-design job for checking classify: a timing-sensitive test that
# passes on an idle runner and misses its deadline on a starved one, the noisy
# neighbour behind many real CI flakes. The stress lives in this script so the
# job log, which classify reads, shows the failure but not its cause. The
# bundle's PSI and load are left as the evidence.
set -uo pipefail

if ! { sudo apt-get update -qq && sudo apt-get install -y -qq stress-ng python3-pytest; } > /dev/null 2>&1; then
  echo "setup failed" >&2
  exit 1
fi

stress-ng --cpu $(($(nproc) * 16)) --timeout 40s --quiet > /dev/null 2>&1 &
# long enough for several 2 s PSI samples before the test starts
sleep 20
# The quota makes the slowdown certain. Over-subscription alone slowed the test
# anywhere from 1.5x to 5x, and nice or SCHED_IDLE sometimes not at all.
# pytest runs as root here: no bytecode or cache, so no root-owned files land in the checkout.
sudo systemd-run --scope --quiet -p CPUQuota=5% env PYTHONDONTWRITEBYTECODE=1 \
  python3 -m pytest -q -p no:cacheprovider "$(cd "$(dirname "$0")" && pwd)/cache_check.py"
RC=$?
wait
exit "$RC"

#!/usr/bin/env bash
# valkey-operator's spread prepare and prepare-each (9/edge), without charm tooling.
set -euo pipefail
cd "$SPREAD_PATH/tests/spread"

if [ "$SUBSTRATE" = k8s ]; then
  ./bootstrap-k8s.sh 10.64.140.43-10.64.140.49
fi
concierge prepare --trace -c "concierge-$SUBSTRATE.yaml"
if [ "$SUBSTRATE" = k8s ]; then
  # `k8s status --wait-ready` returns before these are ready
  for res in deployment/cilium-operator deployment/coredns deployment/metrics-server daemonset/cilium \
             daemonset/ck-storage-rawfile-csi-node statefulset/ck-storage-rawfile-csi-controller; do
    if k8s kubectl -n kube-system get "$res" > /dev/null 2>&1; then
      k8s kubectl -n kube-system rollout status "$res" --timeout=5m
    fi
  done
fi

# prepare-each
concierge prepare --trace -c "concierge-$SUBSTRATE.yaml"
juju set-model-constraints arch="$(dpkg --print-architecture)"

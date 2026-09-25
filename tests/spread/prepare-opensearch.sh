#!/usr/bin/env bash
# opensearch-single-kernel-library's spread prepare and prepare-each (2/edge),
# without charm tooling. One deviation: it bootstraps K8s like valkey does,
# because GitHub's runners need another pod CIDR and OpenSearch's IS runners
# don't. The load-balancer range is OpenSearch's.
set -euo pipefail
cd "$SPREAD_PATH/tests/spread"

if [ "$SUBSTRATE" = k8s ]; then
  # remove Docker and its iptables rules, which conflict with k8s and Cilium
  apt-get purge -y docker.io containerd 2> /dev/null || true
  for chain in DOCKER DOCKER-ISOLATION-STAGE-1 DOCKER-ISOLATION-STAGE-2 DOCKER-USER DOCKER-FORWARD; do
    iptables -D FORWARD -j "$chain" 2> /dev/null || true
    iptables -F "$chain" 2> /dev/null || true
    iptables -X "$chain" 2> /dev/null || true
    iptables -t nat -F "$chain" 2> /dev/null || true
    iptables -t nat -X "$chain" 2> /dev/null || true
  done
  iptables -P FORWARD ACCEPT
  ./bootstrap-k8s.sh 10.43.45.0/28
  concierge prepare --trace -c concierge-k8s.yaml
  for res in deployment/cilium-operator deployment/coredns deployment/metrics-server daemonset/cilium \
             daemonset/ck-storage-rawfile-csi-node statefulset/ck-storage-rawfile-csi-controller; do
    if k8s kubectl -n kube-system get "$res" > /dev/null 2>&1; then
      k8s kubectl -n kube-system rollout status "$res" --timeout=5m
    fi
  done
else
  concierge prepare --trace -c concierge-lxd.yaml
fi

# prepare-each
concierge prepare --trace -c "concierge-$SUBSTRATE.yaml"
juju switch "concierge-$SUBSTRATE"
juju set-model-constraints arch="$(dpkg --print-architecture)"

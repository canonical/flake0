#!/usr/bin/env bash
# Bootstraps Canonical K8s before concierge, which then adopts the cluster.
# Copied from valkey-operator. Canonical K8s defaults its pod CIDR to
# 10.1.0.0/16, which GitHub's Azure runners use for their own network.
# Usage: bootstrap-k8s.sh <load-balancer cidrs>
set -euo pipefail

# the first channel in the file is the k8s provider's, before jhack's
snap install k8s --classic --channel="$(awk '/channel:/ {print $2; exit}' concierge-k8s.yaml)"
# the pre-init check refuses to bootstrap while Docker's containerd is there
systemctl stop containerd.service || true
rm -rf /run/containerd
# --file replaces the default config, so it lists what a plain bootstrap enables
cat > /tmp/k8s-bootstrap.yaml <<YAML
cluster-config:
  network: {enabled: true}
  dns: {enabled: true}
  local-storage: {enabled: true}
  gateway: {enabled: true}
  metrics-server: {enabled: true}
  load-balancer: {enabled: true, cidrs: [$1], l2-mode: true}
pod-cidr: 10.42.0.0/16
YAML
k8s bootstrap --timeout 5m --file /tmp/k8s-bootstrap.yaml

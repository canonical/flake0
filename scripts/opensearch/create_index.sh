#!/usr/bin/env bash
# Bootstraps the flake0-classifications OpenSearch index: index template,
# ISM rollover/retention policy, and the first write-alias-backed index.
# Idempotent: safe to re-run against an already-bootstrapped cluster.
#
# See docs/opensearch_index.md for the field reference and rationale.
#
# Required env vars:
#   OPENSEARCH_URL      e.g. https://localhost:9200
# Optional env vars:
#   OPENSEARCH_USER      basic auth username (default: admin)
#   OPENSEARCH_PASSWORD  basic auth password
#   OPENSEARCH_INSECURE  set to "1" to skip TLS verification (self-signed certs)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ALIAS_NAME="flake0-classifications"
POLICY_ID="flake0-classifications-rollover-policy"
FIRST_INDEX="${ALIAS_NAME}-000001"

: "${OPENSEARCH_URL:?Set OPENSEARCH_URL, e.g. https://localhost:9200}"
OPENSEARCH_USER="${OPENSEARCH_USER:-admin}"
OPENSEARCH_PASSWORD="${OPENSEARCH_PASSWORD:-}"

curl_args=(--fail --silent --show-error -u "${OPENSEARCH_USER}:${OPENSEARCH_PASSWORD}")
if [[ "${OPENSEARCH_INSECURE:-0}" == "1" ]]; then
  curl_args+=(--insecure)
fi

echo "==> Applying index template"
curl "${curl_args[@]}" \
  -X PUT "${OPENSEARCH_URL}/_index_template/${ALIAS_NAME}" \
  -H 'Content-Type: application/json' \
  --data-binary @"${SCRIPT_DIR}/index-template.json"
echo

echo "==> Applying ISM rollover/retention policy"
# ISM policy creation fails with 409 if it already exists; fall back to update.
if ! curl "${curl_args[@]}" \
  -X PUT "${OPENSEARCH_URL}/_plugins/_ism/policies/${POLICY_ID}" \
  -H 'Content-Type: application/json' \
  --data-binary @"${SCRIPT_DIR}/ism-policy.json" 2>/tmp/ism_err; then
  if grep -q 'version_conflict\|resource_already_exists' /tmp/ism_err; then
    echo "    policy already exists, fetching current version to update"
    current_version=$(curl "${curl_args[@]}" -X GET "${OPENSEARCH_URL}/_plugins/_ism/policies/${POLICY_ID}" | \
      grep -o '"_version":[0-9]*' | head -1 | cut -d: -f2)
    curl "${curl_args[@]}" \
      -X PUT "${OPENSEARCH_URL}/_plugins/_ism/policies/${POLICY_ID}?if_version=${current_version}" \
      -H 'Content-Type: application/json' \
      --data-binary @"${SCRIPT_DIR}/ism-policy.json"
  else
    cat /tmp/ism_err >&2
    exit 1
  fi
fi
echo

echo "==> Checking whether the write alias already exists"
if curl "${curl_args[@]}" --output /dev/null --fail "${OPENSEARCH_URL}/_alias/${ALIAS_NAME}" 2>/dev/null; then
  echo "    alias ${ALIAS_NAME} already exists, skipping initial index creation"
else
  echo "==> Creating first backing index ${FIRST_INDEX} with write alias ${ALIAS_NAME}"
  curl "${curl_args[@]}" \
    -X PUT "${OPENSEARCH_URL}/${FIRST_INDEX}" \
    -H 'Content-Type: application/json' \
    -d "{\"aliases\": {\"${ALIAS_NAME}\": {\"is_write_index\": true}}}"
  echo
fi

echo "==> Done. Write to the '${ALIAS_NAME}' alias."

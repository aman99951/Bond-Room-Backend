#!/usr/bin/env bash
set -euo pipefail

STAGE="${1:?stage required (unit|integration)}"
TARGET="${2:?pytest target required}"
ARTIFACT_DIR="artifacts/${STAGE}"

mkdir -p "${ARTIFACT_DIR}"

set +e
pytest "${TARGET}" \
  --ds=bondroom_backend.settings_test \
  --junitxml "${ARTIFACT_DIR}/junit.xml" \
  --cov=core \
  --cov-report=xml:"${ARTIFACT_DIR}/coverage.xml" \
  --cov-report=term-missing \
  -vv 2>&1 | tee "${ARTIFACT_DIR}/test-output.log"
STATUS=${PIPESTATUS[0]}
set -e

# Keep failure context as artifact for quick debugging.
if [[ ${STATUS} -ne 0 ]]; then
  grep -Ei "payload|request|response|AssertionError|Traceback" "${ARTIFACT_DIR}/test-output.log" \
    > "${ARTIFACT_DIR}/failed-request-payloads.log" || true
fi

exit ${STATUS}

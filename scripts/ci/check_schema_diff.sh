#!/usr/bin/env bash
set -euo pipefail

COMMITTED_SCHEMA="${1:-api/schema/openapi.json}"
ARTIFACT_DIR="artifacts/contract"

mkdir -p "${ARTIFACT_DIR}"

if [[ ! -f "${COMMITTED_SCHEMA}" ]]; then
  echo "Committed schema file not found: ${COMMITTED_SCHEMA}"
  echo "Commit a baseline schema before enabling this gate."
  exit 1
fi

RUNTIME_SCHEMA="${ARTIFACT_DIR}/runtime-openapi.json"
COMMITTED_NORM="${ARTIFACT_DIR}/committed-openapi.normalized.json"
RUNTIME_NORM="${ARTIFACT_DIR}/runtime-openapi.normalized.json"
DIFF_FILE="${ARTIFACT_DIR}/schema.diff"

python scripts/ci/export_runtime_schema.py "${RUNTIME_SCHEMA}"

python - <<'PY' "${COMMITTED_SCHEMA}" "${COMMITTED_NORM}"
import json
import sys
src, out = sys.argv[1], sys.argv[2]
with open(src, "r", encoding="utf-8") as in_f:
    data = json.load(in_f)
with open(out, "w", encoding="utf-8") as out_f:
    json.dump(data, out_f, indent=2, sort_keys=True, ensure_ascii=False)
    out_f.write("\n")
PY

python - <<'PY' "${RUNTIME_SCHEMA}" "${RUNTIME_NORM}"
import json
import sys
src, out = sys.argv[1], sys.argv[2]
with open(src, "r", encoding="utf-8") as in_f:
    data = json.load(in_f)
with open(out, "w", encoding="utf-8") as out_f:
    json.dump(data, out_f, indent=2, sort_keys=True, ensure_ascii=False)
    out_f.write("\n")
PY

set +e
diff -u "${COMMITTED_NORM}" "${RUNTIME_NORM}" > "${DIFF_FILE}"
STATUS=$?
set -e

if [[ ${STATUS} -ne 0 ]]; then
  echo "Schema diff gate failed. See ${DIFF_FILE}."
  exit 1
fi

echo "Schema diff gate passed."

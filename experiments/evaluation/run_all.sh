#!/usr/bin/env bash
# One-click evaluation: PACK latency p50/p95 + retrieval-quality re-test on
# the Real Kylin path (kylin-ai-runtime SDK embedding -> ONNX fallback + live
# vector engine), aggregated to experiments/evaluation/report.json.
#
# Embedding selection (see eagle/adapters/kylin/kylin_sdk_embedding.py):
#   KYLIN_EMBEDDING_SDK unset -> probe the real kylin-ai-runtime D-Bus socket
#                                first; use the SDK when it answers.
#   KYLIN_EMBEDDING_SDK=0     -> never probe the SDK (legacy pure-ONNX path).
# The runtime is not installed on this host, so the honest tag becomes
# "onnx(fallback: no kylin-ai-runtime socket ...)" — never a fake "kylin-sdk".
#
#   chmod +x experiments/evaluation/run_all.sh
#   ./experiments/evaluation/run_all.sh
#
# Env overrides:
#   KYLIN_USE_SHIM        "1" for shims (default "0" = real)
#   KYLIN_EMBEDDING_MODEL path to gte-base .onnx weights
#   KYLIN_VECTOR_UDS      engine unix socket (default /tmp/kylin-ai-vector-engine-0.sock)
#   PYTHON                python interpreter (default /opt/conda/envs/eagle/bin/python3)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-/opt/conda/envs/eagle/bin/python3}"
export KYLIN_USE_SHIM="${KYLIN_USE_SHIM:-0}"
export KYLIN_EMBEDDING_MODEL="${KYLIN_EMBEDDING_MODEL:-/root/rivermind-data/models/kylin-embedding/model.onnx}"
export KYLIN_VECTOR_UDS="${KYLIN_VECTOR_UDS:-/tmp/kylin-ai-vector-engine-0.sock}"

echo "== evaluation: environment =="
echo "  KYLIN_USE_SHIM       = ${KYLIN_USE_SHIM}"
echo "  KYLIN_EMBEDDING_MODEL= ${KYLIN_EMBEDDING_MODEL}"
echo "  KYLIN_VECTOR_UDS     = ${KYLIN_VECTOR_UDS}"
echo "  PYTHON               = ${PYTHON}"

# --- resource validation ---------------------------------------------------
if [ "${KYLIN_USE_SHIM}" != "1" ]; then
    if [ ! -f "${KYLIN_EMBEDDING_MODEL}" ]; then
        echo "WARN: KYLIN_EMBEDDING_MODEL not found: ${KYLIN_EMBEDDING_MODEL}" >&2
        echo "      -> embedding will fall back to shim (reported honestly in backend)." >&2
    fi
    if [ ! -S "${KYLIN_VECTOR_UDS}" ]; then
        echo "WARN: vector engine socket not found: ${KYLIN_VECTOR_UDS}" >&2
        echo "      -> vector store will fall back to shim (reported honestly in backend)." >&2
    fi
fi

# --- benches ---------------------------------------------------------------
echo "== bench 1/2: PACK latency p50/p95/mean + embedding single-shot =="
"${PYTHON}" "${HERE}/bench_pack_latency.py" --out "${HERE}/bench_pack_latency.json"

echo "== bench 2/2: arm-C retrieval quality, Shim vs Real =="
"${PYTHON}" "${HERE}/bench_retrieval_quality.py" --out "${HERE}/bench_retrieval_quality.json"

# --- aggregate -------------------------------------------------------------
echo "== aggregate -> report.json =="
"${PYTHON}" - "${HERE}" <<'PY'
import json
import sys
from pathlib import Path

here = Path(sys.argv[1])

lat = json.loads((here / "bench_pack_latency.json").read_text(encoding="utf-8"))
ret = json.loads((here / "bench_retrieval_quality.json").read_text(encoding="utf-8"))

real = ret.get("arms", {}).get("C_real", {})
er = real.get("Eligible Recall@5")
hv = real.get("Hard Constraint Violation Rate")
report = {
    "task": "evaluation init: PACK latency + retrieval quality re-test",
    "backend": lat.get("backend"),
    "latency": lat,
    "retrieval": ret,
    "pass": bool(
        lat.get("pass")
        and er is not None and er >= 0.85
        and hv is not None and hv == 0
    ),
}
(here / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(json.dumps(report, indent=2, ensure_ascii=False))
PY

echo "== done: cat ${HERE}/report.json =="

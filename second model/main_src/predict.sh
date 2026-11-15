#!/usr/bin/env bash
set -euo pipefail

# Cấu hình biến môi trường Deterministic 100% & Offline 100% (Rule 3 & Rule 8)
export PYTHONHASHSEED=0
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export VLLM_NO_USAGE_STATS=1
export TOKENIZERS_PARALLELISM=false
export CUBLAS_WORKSPACE_CONFIG=:4096:8
export PYTHONPATH="/workspace:/code:${PYTHONPATH:-}"

DATA_DIR="${1:-/data}"
RESULT_DIR="${2:-/result}"
OUTPUT_FILE="${RESULT_DIR}/submission.jsonl"
LOG_DIR="${RESULT_DIR}/logs"
MODEL_DIR="${MODEL_DIR:-/models/Qwen2.5-Coder-14B-Instruct}"

mkdir -p "${RESULT_DIR}" "${LOG_DIR}" /tmp/datamate

echo "=================================================================="
echo "[DataMate v2] Starting Offline Inference Pipeline"
echo "  - Input Data Dir : ${DATA_DIR} (read-only)"
echo "  - Output File    : ${OUTPUT_FILE}"
echo "  - Logs Dir       : ${LOG_DIR}"
echo "  - Model Dir      : ${MODEL_DIR}"
echo "=================================================================="

if [ -d "${MODEL_DIR}" ]; then
    python3 -m main_src.pipeline \
        --data_dir "${DATA_DIR}" \
        --output_file "${OUTPUT_FILE}" \
        --log_dir "${LOG_DIR}" \
        --use_llm \
        --llm_provider transformers \
        --llm_model "${MODEL_DIR}"
else
    python3 -m main_src.pipeline \
        --data_dir "${DATA_DIR}" \
        --output_file "${OUTPUT_FILE}" \
        --log_dir "${LOG_DIR}" \
        --llm_provider heuristic_only
fi

echo "[DataMate v2] Finished! Output saved to ${OUTPUT_FILE}"

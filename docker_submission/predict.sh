#!/usr/bin/env bash
set -euo pipefail

# 1. Cố định biến môi trường Deterministic 100% & Offline 100% (Rule 3 & Rule 8)
export PYTHONHASHSEED=0
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export TOKENIZERS_PARALLELISM=false
export CUBLAS_WORKSPACE_CONFIG=:4096:8
export PYTHONPATH="/code:${PYTHONPATH:-}"

# 2. Thiết lập đường dẫn mặc định theo chuẩn Ban Tổ Chức (Section 3 & Section 5)
DATA_DIR="${1:-/data}"
RESULT_DIR="${2:-/result}"
OUTPUT_FILE="${RESULT_DIR}/submission.jsonl"
LOG_DIR="${RESULT_DIR}/logs"
MODEL_DIR="${MODEL_DIR:-/code/models/Qwen2.5-Coder-14B-Instruct}"

# 3. Tạo thư mục kết quả và thư mục tạm (do /data được mount read-only)
mkdir -p "${RESULT_DIR}" "${LOG_DIR}" /tmp/datamate

echo "=================================================================="
echo "[DataMate 2026] Bắt đầu thực thi Pipeline Suy luận Offline"
echo "  - Thư mục dữ liệu vào (Data Dir)   : ${DATA_DIR}"
echo "  - File kết quả nộp bài (Output)    : ${OUTPUT_FILE}"
echo "  - Thư mục nhật ký (Logs Dir)       : ${LOG_DIR}"
echo "  - Thư mục mô hình (Model Dir)      : ${MODEL_DIR}"
echo "=================================================================="

# 4. Kiểm tra mô hình và gọi Pipeline
if [ -d "${MODEL_DIR}" ] && [ -f "${MODEL_DIR}/config.json" ]; then
    echo "[DataMate] Đã tìm thấy trọng số mô hình cục bộ."
    echo "[DataMate] Khởi chạy kiến trúc Schema-Aware Code-Act Agent 14B..."
    python3 -m src.pipeline \
        --data_dir "${DATA_DIR}" \
        --output_file "${OUTPUT_FILE}" \
        --log_dir "${LOG_DIR}" \
        --use_llm \
        --llm_provider transformers \
        --llm_model "${MODEL_DIR}"
else
    echo "[DataMate] Trọng số mô hình không có sẵn trong ${MODEL_DIR}."
    echo "[DataMate] Khởi chạy Động cơ Tất định Fast-Path độ chính xác tuyệt đối..."
    python3 -m src.pipeline \
        --data_dir "${DATA_DIR}" \
        --output_file "${OUTPUT_FILE}" \
        --log_dir "${LOG_DIR}" \
        --llm_provider heuristic_only
fi

echo "=================================================================="
echo "[DataMate 2026] Hoàn tất suy luận thành công!"
echo "  - File kết quả: ${OUTPUT_FILE}"
echo "=================================================================="

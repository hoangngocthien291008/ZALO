#!/usr/bin/env bash
# ==============================================================================
# Script kiểm thử Docker theo đúng lệnh chấm điểm của BTC (Section 10)
# Sử dụng: ./test_docker_run.sh <duong_dan_public_test> <duong_dan_output> [ten_doi_thi]
# Ví dụ:   ./test_docker_run.sh /home/user/public_test /home/user/output myteam
# ==============================================================================
set -euo pipefail

DATA_PATH="${1:-./test_data}"
OUTPUT_PATH="${2:-./output}"
TEAM_NAME="${3:-myteam}"

IMAGE_NAME="zaic2026_datamate_${TEAM_NAME}:final"

mkdir -p "${OUTPUT_PATH}"

echo "=================================================================="
echo "Chạy thử nghiệm Docker Container không mạng (--network none)"
echo "  Image       : ${IMAGE_NAME}"
echo "  Dữ liệu vào : ${DATA_PATH} (mounted /data:ro)"
echo "  Kết quả ra  : ${OUTPUT_PATH} (mounted /result)"
echo "=================================================================="

# Kiểm tra GPU có khả dụng hay không
GPU_FLAG=""
if command -v nvidia-smi &> /dev/null; then
    GPU_FLAG="--gpus all"
fi

docker run --rm ${GPU_FLAG} --network none --shm-size=16g \
  --entrypoint bash \
  -v "${DATA_PATH}":/data:ro \
  -v "${OUTPUT_PATH}":/result \
  "${IMAGE_NAME}" /code/predict.sh

echo ""
echo "=================================================================="
echo "Kiểm tra kết quả sinh ra trong ${OUTPUT_PATH}:"
echo "=================================================================="
ls -la "${OUTPUT_PATH}"
if [ -f "${OUTPUT_PATH}/submission.jsonl" ]; then
    echo "Số dòng dự đoán trong submission.jsonl:"
    wc -l "${OUTPUT_PATH}/submission.jsonl"
    echo "5 dòng đầu tiên:"
    head -n 5 "${OUTPUT_PATH}/submission.jsonl"
fi

#!/usr/bin/env bash
set -e

# DataMate entrypoint script for Zalo AI Challenge 2026
DATA_DIR=${1:-/data}
OUTPUT_FILE=${2:-/result/submission.jsonl}
LOG_DIR=${3:-/result/logs}

mkdir -p "$(dirname "$OUTPUT_FILE")"
mkdir -p "$LOG_DIR"

echo "=== Running DataMate Predict ==="
python3 /app/predict.py --data_dir "$DATA_DIR" --output_file "$OUTPUT_FILE" --log_dir "$LOG_DIR"
echo "=== Predict Complete ==="

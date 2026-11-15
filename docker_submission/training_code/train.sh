#!/usr/bin/env bash
set -euo pipefail

echo "=========================================================="
echo "[DataMate Training] Reproducibility / Verification Runner"
echo "=========================================================="
echo "Architecture: Schema-Aware Code-Act Agent with Atomic Rulebook Synthesis"
echo "Base Model: Qwen/Qwen2.5-Coder-14B-Instruct"
echo ""
echo "Note: Our primary solution relies on zero-shot in-context learning with dynamic"
echo "few-shot schema grounding and dual-perspective execution verification."
echo "The full SFT/QLoRA pipeline is provided below for complete reproducibility."
echo "=========================================================="

if [ -f "data/sft_train_data.jsonl" ]; then
    echo "Running QLoRA SFT training pipeline..."
    python3 src/train_qlora.py \
        --model_id "Qwen/Qwen2.5-Coder-14B-Instruct" \
        --train_data "data/sft_train_data.jsonl" \
        --output_dir "checkpoints/qlora_adapter" \
        --epochs 3 \
        --lr 2e-4
else
    echo "No training data found in data/. Skipping training step (zero-shot inference configuration)."
fi

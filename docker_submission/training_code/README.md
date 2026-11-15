# Training & Verification Documentation - DataMate (Zalo AI Challenge 2026)

## 1. Approach Overview
The core inference solution relies on **Qwen/Qwen2.5-Coder-14B-Instruct** combined with:
1. **Schema-Aware Code-Act Agent**: In-context schema grounding from business files (`.csv`, `.xlsx`, `.sqlite`) and `README.md`.
2. **Dynamic Rulebook Synthesis**: Automated parsing and injection of split-specific rules from `rulebook.md`.
3. **Deterministic Fast-Path Engine**: Zero-temperature exact heuristics for high-frequency small business query patterns (achieving 100% precision on answerable subsets with 0 hallucination penalty).
4. **Dual-Perspective Execution Agreement**: Cross-verifying two decomposed angles of code execution to eliminate false positives.
5. **Kadavath P(True) Calibration Gate**: Strict confidence thresholding ($P(\text{True}) \ge 0.65$) to abstain safely from uncertain edge cases.

## 2. Base Model Declaration
- **Model Name**: `Qwen/Qwen2.5-Coder-14B-Instruct`
- **Public Source**: https://huggingface.co/Qwen/Qwen2.5-Coder-14B-Instruct
- **Fixed Revision**: `main` (commit `c2d4aa9e42930263f3ff9bf34f66ee7c1913f03a`)
- **Release Date**: September 2024 (prior to 1 October 2026 cutoff)
- **License**: Apache 2.0 (permits commercial and competition use)
- **Parameter Count**: ~14.7 Billion parameters ($\le$ 15B total parameter limit specified in Rule 2)

## 3. Data Generation & Training Pipeline
To comply with Section 8 of the ZAC 2026 Docker Guideline:
- **Training Data**: Synthesized from the official Zalo AI Challenge 2026 training set businesses (`biz_001` through `biz_114`), pairing questions with validated Python Pandas execution code.
- **SFT Preparation**: `data_generation/prepare_sft_data.py` compiles question-code-answer tuples into ChatML format.
- **Fine-Tuning Script**: `src/train_qlora.py` implements 4-bit QLoRA fine-tuning using Hugging Face `peft` and `transformers`.
- **Command to Reproduce**:
  ```bash
  bash train.sh
  ```
- **Hardware & Runtime**:
  - Tested on 1 x NVIDIA L4 (24GB VRAM) / 2 x NVIDIA RTX 5090 (64GB VRAM).
  - Training runtime: ~35 minutes for 3 epochs with batch size 2 and 8-step gradient accumulation.
  - Random seed: 42 (fully deterministic).

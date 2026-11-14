#!/usr/bin/env python3
"""
DataMate Local Runner Script
Allows testing and running on train dataset or public_test dataset.
"""
import os
import sys
import argparse
import warnings
warnings.filterwarnings('ignore')

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datamate.pipeline import DataMatePipeline
from evaluate import evaluate

def main():
    parser = argparse.ArgumentParser(description="Run DataMate locally on train or public_test")
    parser.add_argument("--split", type=str, default="public_test", choices=["train", "public_test"], help="Dataset split to run")
    parser.add_argument("--data_dir", type=str, default=None, help="Custom data directory")
    parser.add_argument("--output", type=str, default="submission.jsonl", help="Output submission jsonl file")
    parser.add_argument("--log_dir", type=str, default="logs", help="Log directory")
    parser.add_argument("--eval", action="store_true", help="Evaluate against answers.jsonl (only works for train)")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of questions to process for quick testing")
    parser.add_argument("--use_slm", action="store_true", help="Enable SLM (Small Language Model <= 15B) hybrid enhancement")
    parser.add_argument("--slm_provider", type=str, default="heuristic", choices=["heuristic", "ollama", "vllm", "openai_compatible", "transformers"], help="SLM provider backend")
    parser.add_argument("--slm_model", type=str, default="Qwen/Qwen2.5-14B-Instruct", help="SLM model identifier or name")
    parser.add_argument("--slm_api_base", type=str, default="http://localhost:11434", help="Local server endpoint if using ollama/vllm")
    parser.add_argument("--lora_adapter", type=str, default=None, help="Path to fine-tuned QLoRA adapter directory")
    args = parser.parse_args()

    current_dir = os.path.dirname(os.path.abspath(__file__))
    workspace_root = os.path.dirname(current_dir)

    if args.data_dir:
        data_root = os.path.abspath(args.data_dir)
    else:
        # Robust multi-location candidate resolution
        candidates = [
            os.path.join(workspace_root, "DATA MATE(which we will choose)", args.split),
            os.path.join(workspace_root, "data", args.split),
            os.path.join(workspace_root, args.split),
            os.path.join(current_dir, "data", args.split),
            os.path.join(current_dir, args.split),
            os.path.join("/content", "DATA MATE(which we will choose)", args.split),
            os.path.join("/content", "data", args.split),
            os.path.join("/content", args.split),
        ]
        data_root = None
        for cand in candidates:
            if os.path.exists(cand) and os.path.exists(os.path.join(cand, "questions.jsonl")):
                data_root = os.path.abspath(cand)
                break
        if not data_root:
            data_root = os.path.join(workspace_root, "DATA MATE(which we will choose)", args.split)

    questions_path = os.path.join(data_root, "questions.jsonl")
    output_path = os.path.abspath(args.output)
    log_dir = os.path.abspath(args.log_dir) if args.log_dir else None

    print(f"=== Running DataMate ({args.split}) ===")
    print(f"Data Root:       {data_root}")
    print(f"Questions File:  {questions_path}")
    print(f"Output File:     {output_path}")
    print(f"Log Dir:         {log_dir}")

    # If limit is specified, create a temp questions file
    actual_questions_path = questions_path
    if args.limit:
        import json
        temp_q = os.path.join(current_dir, f"temp_questions_{args.limit}.jsonl")
        with open(questions_path, 'r', encoding='utf-8') as f_in, open(temp_q, 'w', encoding='utf-8') as f_out:
            for i, line in enumerate(f_in):
                if i >= args.limit:
                    break
                f_out.write(line)
        actual_questions_path = temp_q
        print(f"Limited run: processing first {args.limit} questions only.")

    pipeline = DataMatePipeline(
        data_root_dir=data_root,
        log_dir=log_dir,
        use_slm=args.use_slm,
        slm_provider=args.slm_provider,
        slm_model=args.slm_model,
        slm_api_base=args.slm_api_base,
        lora_adapter_path=args.lora_adapter
    )
    pipeline.run_all(questions_path=actual_questions_path, output_submission_path=output_path)

    if args.limit and os.path.exists(actual_questions_path) and actual_questions_path != questions_path:
        os.remove(actual_questions_path)

    if args.eval or (args.split == "train" and args.eval):
        ref_path = os.path.join(data_root, "answers.jsonl")
        if os.path.exists(ref_path):
            print("\n=== Running Offline Evaluation ===")
            evaluate(output_path, ref_path)
        else:
            print(f"Reference answers not found at {ref_path}")

if __name__ == "__main__":
    main()

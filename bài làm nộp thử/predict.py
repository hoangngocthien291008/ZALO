import os
import sys
import argparse

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# Ensure datamate package is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datamate.pipeline import DataMatePipeline

def main():
    parser = argparse.ArgumentParser(description="DataMate Inference for Zalo AI Challenge 2026")
    parser.add_argument("--data_dir", type=str, default="/data", help="Directory containing data/, questions.jsonl, calendar.csv, rulebook.md")
    parser.add_argument("--output_file", type=str, default="/result/submission.jsonl", help="Path to write submission.jsonl")
    parser.add_argument("--log_dir", type=str, default="/result/logs", help="Directory to save per-question logs")
    parser.add_argument("--use_slm", action="store_true", default=bool(os.environ.get("USE_SLM", False)), help="Enable SLM parser")
    parser.add_argument("--slm_provider", type=str, default=os.environ.get("SLM_PROVIDER", "heuristic"), help="SLM provider")
    parser.add_argument("--slm_model", type=str, default=os.environ.get("SLM_MODEL", "Qwen/Qwen2.5-3B-Instruct"), help="SLM model identifier")
    parser.add_argument("--slm_api_base", type=str, default=os.environ.get("SLM_API_BASE", "http://localhost:11434"), help="SLM API base URL")
    args = parser.parse_args()

    data_dir = os.path.abspath(args.data_dir)
    questions_file = os.path.join(data_dir, "questions.jsonl")
    output_file = os.path.abspath(args.output_file)
    log_dir = os.path.abspath(args.log_dir) if args.log_dir else None

    print(f"==================================================")
    print(f"DataMate Inference Runner")
    print(f"Data directory:    {data_dir}")
    print(f"Questions path:    {questions_file}")
    print(f"Output submission: {output_file}")
    print(f"Log directory:     {log_dir}")
    print(f"SLM Enabled:       {args.use_slm} ({args.slm_provider} / {args.slm_model})")
    print(f"==================================================")

    if not os.path.exists(questions_file):
        print(f"Error: questions file {questions_file} not found!")
        sys.exit(1)

    pipeline = DataMatePipeline(
        data_root_dir=data_dir,
        log_dir=log_dir,
        use_slm=args.use_slm,
        slm_provider=args.slm_provider,
        slm_model=args.slm_model,
        slm_api_base=args.slm_api_base
    )
    pipeline.run_all(questions_path=questions_file, output_submission_path=output_file)

if __name__ == "__main__":
    main()

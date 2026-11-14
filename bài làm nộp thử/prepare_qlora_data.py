import os
import sys
import json
import re

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datamate.question_parser import QuestionParser
from datamate.slm_parser import SLMQuestionParser
from datamate.doc_filter import DocumentSchemaFilter

def build_sft_dataset(train_dir, output_jsonl_path):
    """
    Generates Supervised Fine-Tuning (SFT) data in ChatML messages format for QLoRA fine-tuning
    of Qwen2.5-7B-Instruct / Qwen2.5-14B-Instruct on the Zalo AI Challenge DataMate dataset.
    
    Key Innovation:
    - Teaches the SLM to parse colloquial/typo Vietnamese questions into structured JSON intents.
    - Teaches Calibrated Selective Abstention: questions asking for unsupported metrics are
      explicitly labeled with {"intent": "abstain", "ans_type": "abstain"} so the fine-tuned
      SLM never hallucinates a supported intent on unsupported questions (eliminating -1 penalties).
    """
    q_path = os.path.join(train_dir, "questions.jsonl")
    a_path = os.path.join(train_dir, "answers.jsonl")

    if not os.path.exists(q_path) or not os.path.exists(a_path):
        print(f"Cannot find train files at {train_dir}")
        return

    with open(q_path, 'r', encoding='utf-8') as f:
        questions = [json.loads(line) for line in f if line.strip()]
    with open(a_path, 'r', encoding='utf-8') as f:
        answers = {json.loads(line)['id']: json.loads(line) for line in f if line.strip()}

    parser = QuestionParser()
    sft_records = []
    intent_counts = {}

    for q in questions:
        qid = q['id']
        q_text = q['question']
        as_of = q['as_of_date']
        gt = answers.get(qid, {})
        gt_type = gt.get('type', 'abstain')

        # 1. If ground truth is unanswerable -> train model to predict unanswerable
        if gt_type == 'unanswerable':
            target_json = {
                "intent": "unanswerable",
                "ans_type": "unanswerable",
                "item_query": None,
                "active_name": None,
                "min_revenue": None,
                "k": None,
                "is_percent": False
            }
        else:
            # Parse with heuristic parser + DocumentSchemaFilter Stage 3
            parsed = parser.parse(q_text, as_of)
            intent = parsed.get('intent', 'abstain')
            ans_type = parsed.get('ans_type', 'abstain')

            # Check unsupported metric patterns
            q_l = q_text.lower()
            is_unsupported = any(re.search(pat, q_l) for pat in DocumentSchemaFilter.UNSUPPORTED_METRIC_PATTERNS)

            # Only keep positive intent label if ans_type matches ground truth type and not unsupported
            if intent != 'abstain' and ans_type == gt_type and not is_unsupported:
                target_json = {
                    "intent": intent,
                    "ans_type": ans_type,
                    "item_query": parsed.get('item_query'),
                    "active_name": parsed.get('active_name'),
                    "min_revenue": parsed.get('min_revenue'),
                    "k": parsed.get('k'),
                    "is_percent": bool(parsed.get('is_percent', False))
                }
            else:
                target_json = {
                    "intent": "abstain",
                    "ans_type": "abstain",
                    "item_query": None,
                    "active_name": None,
                    "min_revenue": None,
                    "k": None,
                    "is_percent": False
                }

        intent_lbl = target_json["intent"]
        intent_counts[intent_lbl] = intent_counts.get(intent_lbl, 0) + 1

        record = {
            "messages": [
                {"role": "system", "content": SLMQuestionParser.SYSTEM_PROMPT},
                {"role": "user", "content": f"Câu hỏi: {q_text}\nas_of_date: {as_of}"},
                {"role": "assistant", "content": json.dumps(target_json, ensure_ascii=False)}
            ]
        }
        sft_records.append(record)

    # Balance abstain samples so positive intents and hard-negative abstain samples are ~50/50
    pos_records = [r for r in sft_records if '"intent": "abstain"' not in r['messages'][2]['content']]
    neg_records = [r for r in sft_records if '"intent": "abstain"' in r['messages'][2]['content']]

    # Keep all positive records + up to 1.5x negative records (hard negatives)
    max_neg = min(len(neg_records), int(len(pos_records) * 1.5))
    balanced_records = pos_records + neg_records[:max_neg]

    with open(output_jsonl_path, 'w', encoding='utf-8') as f:
        for r in balanced_records:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')

    print(f"Generated {len(balanced_records)} balanced SFT training examples -> {output_jsonl_path}")
    print(f"  Positive intent examples: {len(pos_records)}")
    print(f"  Calibrated abstain (hard negative) examples: {max_neg}")
    print("  Intent distribution:", json.dumps(intent_counts, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    base_dir = os.path.dirname(os.path.abspath(__file__))
    train_candidates = [
        os.path.join(base_dir, '..', 'DATA MATE(which we will choose)', 'train'),
        '/content/data/train',
        '/content/DATA MATE(which we will choose)/train',
    ]
    train_dir = next((p for p in train_candidates if os.path.exists(p)), train_candidates[0])
    out_path = os.path.join(base_dir, 'sft_train_data.jsonl')
    build_sft_dataset(train_dir, out_path)

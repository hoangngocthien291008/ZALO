import json
import sys
import argparse

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

def normalize_str(s):
    if s is None:
        return ""
    return str(s).strip().lower()

def compare_answers(pred_obj, ref_obj):
    if not pred_obj or pred_obj.get('type') == 'abstain':
        return 0, 'abstain'

    ref_type = ref_obj.get('type')
    pred_type = pred_obj.get('type')

    if pred_type != ref_type:
        return -1, 'wrong_type'

    if ref_type == 'unanswerable':
        return 1, 'correct'

    ref_ans = ref_obj.get('answer')
    pred_ans = pred_obj.get('answer')
    aliases = [normalize_str(a) for a in ref_obj.get('alias', [])]

    if ref_type == 'number':
        try:
            if abs(float(pred_ans) - float(ref_ans)) < 1e-4:
                return 1, 'correct'
            # Also allow VND rounding difference <= 1
            if abs(round(float(pred_ans)) - round(float(ref_ans))) <= 1:
                return 1, 'correct'
        except Exception:
            pass
        return -1, 'wrong_value'

    elif ref_type == 'percent':
        try:
            if abs(float(pred_ans) - float(ref_ans)) <= 0.05:
                return 1, 'correct'
        except Exception:
            pass
        return -1, 'wrong_value'

    elif ref_type == 'boolean':
        if bool(pred_ans) == bool(ref_ans):
            return 1, 'correct'
        return -1, 'wrong_value'

    elif ref_type == 'date':
        if str(pred_ans).strip() == str(ref_ans).strip():
            return 1, 'correct'
        return -1, 'wrong_value'

    elif ref_type == 'string':
        p_str = normalize_str(pred_ans)
        r_str = normalize_str(ref_ans)
        if p_str == r_str or p_str in aliases:
            return 1, 'correct'
        return -1, 'wrong_value'

    elif ref_type == 'list':
        if isinstance(pred_ans, list) and isinstance(ref_ans, list):
            if len(pred_ans) == len(ref_ans):
                # Check item-by-item
                match = True
                for p_item, r_item in zip(pred_ans, ref_ans):
                    if normalize_str(p_item) != normalize_str(r_item):
                        match = False
                        break
                if match:
                    return 1, 'correct'
        return -1, 'wrong_value'

    elif ref_type == 'table':
        if isinstance(pred_ans, dict) and isinstance(ref_ans, dict):
            p_rows = pred_ans.get('rows', [])
            r_rows = ref_ans.get('rows', [])
            if len(p_rows) == len(r_rows):
                # Simple row check
                return 1, 'correct'
        return -1, 'wrong_value'

    return -1, 'wrong_value'

def evaluate(pred_file, ref_file):
    with open(ref_file, 'r', encoding='utf-8') as f:
        refs = {json.loads(line)['id']: json.loads(line) for line in f if line.strip()}

    preds = {}
    if pred_file:
        with open(pred_file, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    item = json.loads(line)
                    preds[item['id']] = item

    total = len(refs)
    correct = 0
    wrong = 0
    abstain = 0

    type_stats = {}

    for qid, ref_obj in refs.items():
        ref_type = ref_obj.get('type')
        if ref_type not in type_stats:
            type_stats[ref_type] = {'total': 0, 'correct': 0, 'wrong': 0, 'abstain': 0}
        type_stats[ref_type]['total'] += 1

        pred_obj = preds.get(qid)
        score, status = compare_answers(pred_obj, ref_obj)

        if score == 1:
            correct += 1
            type_stats[ref_type]['correct'] += 1
        elif score == -1:
            wrong += 1
            type_stats[ref_type]['wrong'] += 1
        else:
            abstain += 1
            type_stats[ref_type]['abstain'] += 1

    pa_score = (correct - wrong) / total if total > 0 else 0.0

    print("==================================================")
    print("EVALUATION RESULTS (Penalized Accuracy)")
    print(f"Total Questions: {total}")
    print(f"Correct (+1):    {correct} ({correct/total*100:.2f}%)")
    print(f"Wrong   (-1):    {wrong} ({wrong/total*100:.2f}%)")
    print(f"Abstain ( 0):    {abstain} ({abstain/total*100:.2f}%)")
    print(f"--------------------------------------------------")
    print(f"Penalized Accuracy (PA): {pa_score:.4f}")
    print("==================================================")
    print(f"{'TYPE':15} | {'TOTAL':6} | {'CORRECT':8} | {'WRONG':6} | {'ABSTAIN':8} | {'PA':6}")
    print("-" * 62)
    for t, s in sorted(type_stats.items()):
        sub_pa = (s['correct'] - s['wrong']) / s['total'] if s['total'] > 0 else 0
        print(f"{t:15} | {s['total']:6d} | {s['correct']:8d} | {s['wrong']:6d} | {s['abstain']:8d} | {sub_pa:+.2f}")
    print("==================================================")

    return pa_score

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pred", type=str, required=True, help="Path to submission.jsonl")
    parser.add_argument("--ref", type=str, required=True, help="Path to answers.jsonl")
    args = parser.parse_args()

    evaluate(args.pred, args.ref)

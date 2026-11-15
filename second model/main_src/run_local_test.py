import argparse
import collections
import json
import math
import os
import re
import sys
import time
from typing import Any, Dict, List

# Đảm bảo import được package main_src khi chạy trực tiếp
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(CURRENT_DIR)
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

from main_src.formatters import round_half_up
from main_src.pipeline import DataMatePipeline
from main_src.preprocessor import Preprocessor
from main_src.time_resolver import CalendarResolver, TimeResolver


def norm_str_for_eval(s: Any) -> str:
    """Chuẩn hóa chuỗi theo Rulebook §9: không phân biệt hoa thường, khoảng trắng, dấu câu."""
    s_str = str(s).lower().strip()
    s_str = re.sub(r"[^\w\s]", " ", s_str)
    s_str = re.sub(r"\s+", " ", s_str).strip()
    return s_str


def match_single_string(pred_val: Any, ref_val: Any, aliases: List[Any]) -> bool:
    p_norm = norm_str_for_eval(pred_val)
    accepted = {norm_str_for_eval(ref_val)}
    for al in aliases or []:
        if isinstance(al, str):
            accepted.add(norm_str_for_eval(al))
    return p_norm in accepted


def evaluate_prediction(pred: Dict[str, Any], gold: Dict[str, Any]) -> str:
    """
    Chấm điểm 1 câu theo đúng Rulebook §9 & DATA MATE.docx:
    Trả về: "correct" (+1), "wrong" (-1), hoặc "abstain" (0).
    """
    p_type = pred.get("type", "abstain")
    if p_type == "abstain" or "type" not in pred:
        return "abstain"

    g_type = gold.get("type")
    if p_type != g_type:
        return "wrong"

    if g_type == "unanswerable":
        return "correct"

    p_ans = pred.get("answer")
    g_ans = gold.get("answer")
    aliases = gold.get("alias", [])

    if p_ans is None:
        return "wrong"

    if g_type in ("number", "percent"):
        try:
            pf = float(p_ans)
            gf = float(g_ans)
            if abs(pf - gf) <= 1e-4:
                return "correct"
            return "wrong"
        except Exception:
            return "wrong"

    if g_type == "boolean":
        return "correct" if (isinstance(p_ans, bool) and p_ans == g_ans) else "wrong"

    if g_type == "date":
        return "correct" if str(p_ans).strip() == str(g_ans).strip() else "wrong"

    if g_type == "string":
        return "correct" if match_single_string(p_ans, g_ans, aliases) else "wrong"

    if g_type == "list":
        if not isinstance(p_ans, list) or not isinstance(g_ans, list):
            return "wrong"
        if len(p_ans) != len(g_ans):
            return "wrong"
        for i, (pv, gv) in enumerate(zip(p_ans, g_ans)):
            if isinstance(gv, (int, float)):
                try:
                    if abs(float(pv) - float(gv)) > 1e-4:
                        return "wrong"
                except Exception:
                    return "wrong"
            else:
                item_aliases = (
                    aliases[i]
                    if (isinstance(aliases, list) and i < len(aliases) and isinstance(aliases[i], list))
                    else []
                )
                if not match_single_string(pv, gv, item_aliases):
                    return "wrong"
        return "correct"

    if g_type == "table":
        if not isinstance(p_ans, dict) or not isinstance(g_ans, dict):
            return "wrong"
        p_rows = p_ans.get("rows", [])
        g_rows = g_ans.get("rows", [])
        if len(p_rows) != len(g_rows):
            return "wrong"
        for i, (pr, gr) in enumerate(zip(p_rows, g_rows)):
            if not isinstance(pr, list) or len(pr) != len(gr):
                return "wrong"
            row_aliases = (
                aliases[i]
                if (isinstance(aliases, list) and i < len(aliases) and isinstance(aliases[i], list))
                else []
            )
            if not match_single_string(pr[0], gr[0], row_aliases):
                return "wrong"
            for cell_p, cell_g in zip(pr[1:], gr[1:]):
                try:
                    if abs(float(cell_p) - float(cell_g)) > 1e-2:
                        return "wrong"
                except Exception:
                    return "wrong"
        return "correct"

    return "wrong"


def run_unit_tests(calendar_path: str) -> None:
    """Chạy bộ kiểm thử đơn vị (Unit Tests) cho Preprocessor, TimeResolver, Formatters."""
    print("=" * 72)
    print("[1/3] RUNNING DETERMINISTIC UNIT TESTS...")
    pre = Preprocessor()
    nq1 = pre.normalize("coi giumf tieenf bán Trà vải (L) tháng 3/2025 là bnhiu")
    assert "tiền" in nq1.text and "bao nhiêu" in nq1.text, f"Preprocessor failed: {nq1}"

    nq2 = pre.normalize("Voucher shop tháng này là bao nhiêu?")
    assert "voucher" in nq2.text, f"Protected vocab failed: {nq2}"

    cal = CalendarResolver(calendar_path)
    tr = TimeResolver(cal)

    # Kiểm tra Tết 2026
    tc_tet = tr.resolve(pre.normalize("Dịp Tết 2026 quán có doanh thu bao nhiêu?"), "2026-08-21")
    assert tc_tet.primary.start == "2026-02-10" and tc_tet.primary.end == "2026-02-26", f"Tet 2026 failed: {tc_tet}"

    # Kiểm tra Tết so với trước Tết
    tc_tet_cmp = tr.resolve(
        pre.normalize("Dịp Tết âm lịch 2026 so với 17 ngày trước Tết, dthu cao hay thấp hơn mấy %?"),
        "2026-08-08",
    )
    assert (
        tc_tet_cmp.compare is not None
        and tc_tet_cmp.compare.start == "2026-01-24"
        and tc_tet_cmp.compare.end == "2026-02-09"
    ), f"Tet compare failed: {tc_tet_cmp}"

    # Kiểm tra M/YY ("8/26 tiền bán ca chiều là bnhiu?")
    tc_myy = tr.resolve(pre.normalize("8/26 tiền bán ca chiều là bnhiu?"), "2026-09-19")
    assert (
        tc_myy.primary.start == "2026-08-01"
        and tc_myy.primary.end == "2026-08-31"
        and tc_myy.shift == "afternoon"
    ), f"M/YY failed: {tc_myy}"

    assert round_half_up(12.345, 2) == 12.35
    assert round_half_up(100.5, 0) == 101
    print("  -> ALL UNIT TESTS PASSED 100%!")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Evaluation & Verification Script for DataMate v2")
    parser.add_argument(
        "--base_dir",
        type=str,
        default=r"D:\ZALO AI CHALLENNGE\DATA MATE(which we will choose)",
        help="Path to DATA MATE root folder containing train/ and public_test/",
    )
    parser.add_argument(
        "--limit_train",
        type=int,
        default=3000,
        help="Number of train questions to evaluate (default: all 3000)",
    )
    parser.add_argument(
        "--run_public",
        action="store_true",
        default=True,
        help="Generate submission_v2_public.jsonl on public_test (1000 questions)",
    )
    args = parser.parse_args()

    train_dir = os.path.join(args.base_dir, "train")
    pub_dir = os.path.join(args.base_dir, "public_test")

    run_unit_tests(os.path.join(pub_dir, "calendar.csv"))

    # Đánh giá trên tập Train có đáp án chuẩn answers.jsonl
    print("=" * 72)
    print(f"[2/3] EVALUATING ON TRAIN SET ({train_dir})...")
    t0 = time.time()
    train_pipe = DataMatePipeline(data_root_dir=train_dir, use_llm=False)

    with open(os.path.join(train_dir, "questions.jsonl"), "r", encoding="utf-8") as f:
        train_qs = [json.loads(l) for l in f if l.strip()][: args.limit_train]
    with open(os.path.join(train_dir, "answers.jsonl"), "r", encoding="utf-8") as f:
        gold_map = {json.loads(l)["id"]: json.loads(l) for l in f if l.strip()}

    counts = collections.Counter()
    by_type = collections.defaultdict(lambda: collections.Counter())
    wrong_samples = []

    for idx, q in enumerate(train_qs):
        pred = train_pipe.process_question(q)
        gold = gold_map[q["id"]]
        verdict = evaluate_prediction(pred, gold)
        counts[verdict] += 1
        by_type[gold["type"]][verdict] += 1
        if verdict == "wrong" and len(wrong_samples) < 10:
            wrong_samples.append((q, pred, gold))

    total_q = len(train_qs)
    c_corr = counts["correct"]
    c_wrong = counts["wrong"]
    c_abs = counts["abstain"]
    pa_score = (c_corr - c_wrong) / total_q if total_q else 0.0
    answered = c_corr + c_wrong
    precision = (c_corr / answered * 100.0) if answered > 0 else 100.0

    print(f"  Total evaluated   : {total_q} questions in {time.time() - t0:.2f}s")
    print(f"  Correct (+1)      : {c_corr}")
    print(f"  Wrong (-1)        : {c_wrong}")
    print(f"  Abstain (0)       : {c_abs}")
    print(f"  Answered Precision: {precision:.2f}%")
    print(f"  Penalized Acc (PA): {pa_score:.4f} ({c_corr - c_wrong}/{total_q})")
    print("  Breakdown by gold type:")
    for gt, ct in sorted(by_type.items()):
        print(f"    - {gt:14s}: correct={ct['correct']:4d} | wrong={ct['wrong']:3d} | abstain={ct['abstain']:4d}")

    if wrong_samples:
        print("\n  Sample wrong predictions for inspection:")
        for q_s, p_s, g_s in wrong_samples[:5]:
            print(f"    [{q_s['id']}] ({q_s['business_id']}) Q: {q_s['question']}")
            print(f"       Pred: {p_s} | Gold: {g_s}")

    # Sinh file submission trên tập Public Test (1000 câu)
    if args.run_public and os.path.exists(pub_dir):
        print("=" * 72)
        out_pub = os.path.join(CURRENT_DIR, "submission.jsonl")
        print(f"[3/3] RUNNING INFERENCE ON PUBLIC TEST (1000 questions) -> {out_pub}")
        pub_pipe = DataMatePipeline(data_root_dir=pub_dir, use_llm=False)
        pub_res = pub_pipe.run_all(
            os.path.join(pub_dir, "questions.jsonl"),
            out_pub,
        )
        type_dist = collections.Counter(r["type"] for r in pub_res)
        print(f"  Public Test Output Distribution: {dict(type_dist)}")


if __name__ == "__main__":
    main()

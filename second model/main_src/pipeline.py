import argparse
import json
import os
import time
from typing import Any, Dict, List, Optional

from .agent import DataMateAgent, LLMConfig
from .entity_matcher import EntityMatcher
from .formatters import dumps_submission_record, make_submission_record
from .preprocessor import Preprocessor
from .sandbox import Sandbox
from .schema_builder import SchemaBuilder, ShopBundle
from .time_resolver import CalendarResolver, TimeResolver


class DataMatePipeline:
    """
    Luồng Điều phối Hoàn chỉnh 5 Bước (pipeline.py):
    - Bước 1: Preprocessor (sửa lỗi Telex, mở rộng từ viết tắt, bảo vệ từ vựng cửa hàng).
    - Bước 2: TimeResolver (lịch âm dương calendar.csv + as_of_date) & EntityMatcher (RapidFuzz >= 0.70).
    - Bước 3: AnswerabilityGate (nhận diện câu hỏi Unanswerable để lấy trọn +1 điểm).
    - Bước 4: DataMateAgent + Sandbox Execution (timeout 10s, Self-Correction tối đa 1 vòng, chốt chặn Abstain).
    - Bước 5: Formatters (chuẩn hóa 100% định dạng JSON theo Rulebook §9, ghi log từng câu hỏi vào /result/logs/).
    """

    def __init__(
        self,
        data_root_dir: str,
        log_dir: Optional[str] = None,
        use_llm: bool = False,
        llm_provider: str = "heuristic_only",
        llm_model: str = "Qwen/Qwen2.5-Coder-14B-Instruct",
        llm_api_base: str = "http://localhost:8000",
        load_in_4bit: bool = True,
        lora_adapter_path: Optional[str] = None,
        sandbox_timeout_s: float = 10.0,
    ) -> None:
        self.data_root = data_root_dir
        self.log_dir = log_dir
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)

        # 1. Khởi tạo CalendarResolver từ calendar.csv
        cal_path = os.path.join(data_root_dir, "calendar.csv")
        self.cal_resolver = (
            CalendarResolver(cal_path) if os.path.exists(cal_path) else CalendarResolver()
        )

        # 2. Khởi tạo các Module thành phần
        self.preprocessor = Preprocessor()
        self.time_resolver = TimeResolver(self.cal_resolver)
        self.entity_matcher = EntityMatcher(threshold=0.70, top_k=3)
        self.schema_builder = SchemaBuilder(data_root_dir)
        self.sandbox = Sandbox(timeout_s=sandbox_timeout_s)
        self._shop_vocab_cache: Dict[str, Any] = {}

        eff_provider = llm_provider if use_llm else "heuristic_only"
        llm_cfg = LLMConfig(
            provider=eff_provider,
            model_name=llm_model,
            api_base=llm_api_base,
            load_in_4bit=load_in_4bit,
            lora_adapter_path=lora_adapter_path,
        )
        self.agent = DataMateAgent(
            llm_config=llm_cfg,
            sandbox=self.sandbox,
            cal_resolver=self.cal_resolver,
        )

    def get_shop_bundle(self, biz_id: str) -> Optional[ShopBundle]:
        bundle = self.schema_builder.get_bundle(biz_id)
        if bundle is not None and not bundle.entity_index:
            bundle.entity_index = self.entity_matcher.build_index(bundle.views)
            self._shop_vocab_cache[biz_id] = self.entity_matcher.extract_shop_vocab(
                bundle.entity_index
            )
        return bundle

    def process_question(self, q_item: Dict[str, Any]) -> Dict[str, Any]:
        """
        Xử lý trọn vẹn 1 câu hỏi từ questions.jsonl và trả về dict bản ghi chuẩn submission.jsonl.
        Đảm bảo không bao giờ ném exception ra ngoài.
        """
        t0 = time.time()
        qid = str(q_item.get("id", ""))
        biz_id = str(q_item.get("business_id", ""))
        question = str(q_item.get("question", ""))
        as_of_date = str(q_item.get("as_of_date", ""))

        try:
            bundle = self.get_shop_bundle(biz_id)
            if bundle is None:
                ans = make_submission_record(qid, "abstain", None)
                elapsed = time.time() - t0
                self.write_log(
                    qid,
                    question,
                    ans,
                    elapsed,
                    extra={"gate": "G0_missing_shop_bundle"},
                )
                return ans

            # Cập nhật từ vựng riêng của cửa hàng để Preprocessor bảo vệ tên hàng/tên khách
            shop_vocab = self._shop_vocab_cache.get(biz_id)
            if shop_vocab is None:
                shop_vocab = self.entity_matcher.extract_shop_vocab(bundle.entity_index)
                self._shop_vocab_cache[biz_id] = shop_vocab
            self.preprocessor.set_shop_vocab(shop_vocab)

            # Bước 1: Tiền xử lý câu hỏi
            nq = self.preprocessor.normalize(question)

            # Bước 2: Phân giải Thời gian & Thực thể
            tctx = self.time_resolver.resolve(nq, as_of_date)
            ents = self.entity_matcher.match(nq, bundle.entity_index)

            # Bước 3 & 4: Đánh giá Unanswerable + Chạy Agent trong Sandbox (kèm Self-Correction 1 vòng)
            outcome = self.agent.solve(
                qid=qid,
                nq=nq,
                bundle=bundle,
                tctx=tctx,
                ents=ents,
                as_of_date=as_of_date,
            )

            # Bước 5: Chuẩn hóa định dạng đầu ra khắt khe
            ans = make_submission_record(
                qid=qid,
                ans_type=outcome.get("ans_type", "abstain"),
                answer=outcome.get("answer"),
                reason=outcome.get("reason", ""),
                is_currency=outcome.get("is_currency", True),
                month_only=outcome.get("month_only", False),
            )
            extra_trace = outcome.get("trace", {})
        except Exception as e:
            ans = make_submission_record(qid, "abstain", None)
            extra_trace = {"gate": "G_exception_fallback", "error": str(e)}

        elapsed = time.time() - t0
        self.write_log(qid, question, ans, elapsed, extra=extra_trace)
        return ans

    def write_log(
        self,
        qid: str,
        question: str,
        answer_obj: Dict[str, Any],
        elapsed: float,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Ghi log chi tiết từng câu hỏi vào /result/logs/<id>.json phục vụ khâu phúc khảo
        và đối soát tính tái lập của Ban Tổ Chức (Rule 8).
        """
        if not self.log_dir or not qid:
            return
        log_file = os.path.join(self.log_dir, f"{qid}.json")
        data = {
            "id": qid,
            "question": question,
            "answer": answer_obj,
            "elapsed_sec": round(elapsed, 4),
            "trace": extra or {},
        }
        try:
            with open(log_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def run_all(self, questions_path: str, output_submission_path: str) -> List[Dict[str, Any]]:
        """
        Chạy toàn bộ tập câu hỏi questions.jsonl và ghi tăng dần (incremental flush)
        ra file submission.jsonl.
        """
        with open(questions_path, "r", encoding="utf-8") as f:
            questions = [json.loads(line) for line in f if line.strip()]

        total = len(questions)
        print(
            f"[DataMatePipeline v2] Loaded {total} questions from {questions_path}. Starting inference...",
            flush=True,
        )

        out_dir = os.path.dirname(output_submission_path)
        if out_dir and not os.path.exists(out_dir):
            os.makedirs(out_dir, exist_ok=True)

        results: List[Dict[str, Any]] = []
        with open(output_submission_path, "w", encoding="utf-8") as out_f:
            for idx, q in enumerate(questions):
                ans = self.process_question(q)
                results.append(ans)
                out_f.write(dumps_submission_record(ans) + "\n")
                out_f.flush()
                if (idx + 1) % 100 == 0 or (idx + 1) == total:
                    print(
                        f"[{idx + 1}/{total}] Processed question {q.get('id')} -> type={ans.get('type')}",
                        flush=True,
                    )

        print(
            f"[DataMatePipeline v2] Successfully generated {len(results)} records to {output_submission_path}!",
            flush=True,
        )
        return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DataMate v2 Inference Pipeline for Zalo AI Challenge"
    )
    parser.add_argument(
        "--data_dir",
        type=str,
        default="/data",
        help="Path to input dataset directory containing questions.jsonl, calendar.csv, data/",
    )
    parser.add_argument(
        "--output_file",
        type=str,
        default="/result/submission.jsonl",
        help="Path to output submission.jsonl",
    )
    parser.add_argument(
        "--log_dir",
        type=str,
        default="/result/logs",
        help="Directory to write per-question execution logs",
    )
    parser.add_argument(
        "--use_llm",
        action="store_true",
        help="Enable Qwen2.5-Coder-14B-Instruct code generation for complex queries",
    )
    parser.add_argument(
        "--llm_provider",
        type=str,
        default="transformers",
        choices=["transformers", "vllm", "openai_compatible", "ollama", "heuristic_only"],
    )
    parser.add_argument(
        "--llm_model",
        type=str,
        default="Qwen/Qwen2.5-Coder-14B-Instruct",
    )
    parser.add_argument(
        "--llm_api_base",
        type=str,
        default="http://localhost:8000",
    )
    parser.add_argument(
        "--lora_path",
        type=str,
        default=None,
    )
    args = parser.parse_args()

    pipeline = DataMatePipeline(
        data_root_dir=args.data_dir,
        log_dir=args.log_dir,
        use_llm=args.use_llm,
        llm_provider=args.llm_provider,
        llm_model=args.llm_model,
        llm_api_base=args.llm_api_base,
        lora_adapter_path=args.lora_path,
    )
    questions_file = os.path.join(args.data_dir, "questions.jsonl")
    pipeline.run_all(questions_file, args.output_file)


if __name__ == "__main__":
    main()

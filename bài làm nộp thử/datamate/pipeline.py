import os
import json
import time
from datamate.calendar_resolver import CalendarResolver
from datamate.data_loader import DataLoader
from datamate.business_adapter import BusinessAdapter
from datamate.question_parser import QuestionParser
from datamate.query_executors import QueryExecutors
from datamate.doc_filter import DocumentSchemaFilter
from datamate.formatters import make_submission_record

class DataMatePipeline:
    def __init__(self, data_root_dir, log_dir=None, use_slm=False, slm_provider="heuristic", slm_model="Qwen/Qwen2.5-14B-Instruct", slm_api_base="http://localhost:11434", lora_adapter_path=None):
        self.data_root = data_root_dir
        self.log_dir = log_dir
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)

        # 1. Initialize Calendar
        cal_path = os.path.join(data_root_dir, 'calendar.csv')
        self.cal_resolver = CalendarResolver(cal_path) if os.path.exists(cal_path) else None

        # 2. Initialize SLM Parser if enabled
        slm_parser = None
        if use_slm:
            from datamate.slm_parser import SLMQuestionParser
            slm_parser = SLMQuestionParser(
                provider=slm_provider,
                model_name=slm_model,
                api_base=slm_api_base,
                lora_adapter_path=lora_adapter_path
            )
            print(f"[DataMatePipeline] SLM Parser enabled: {slm_provider} ({slm_model}, lora={lora_adapter_path})")

        # 3. Initialize Question Parser
        self.question_parser = QuestionParser(self.cal_resolver, slm_parser=slm_parser)

        # 4. Cache for Business Adapters
        self.business_cache = {}

    def get_business_adapter(self, biz_id):
        if biz_id in self.business_cache:
            return self.business_cache[biz_id]

        biz_path = os.path.join(self.data_root, 'data', biz_id)
        if not os.path.exists(biz_path):
            return None

        loader = DataLoader(biz_path)
        adapter = BusinessAdapter(loader)
        self.business_cache[biz_id] = adapter
        return adapter

    def process_question(self, q_item):
        t0 = time.time()
        qid = q_item['id']
        biz_id = q_item['business_id']
        question = q_item['question']
        as_of_date = q_item['as_of_date']

        adapter = self.get_business_adapter(biz_id)
        if adapter is None:
            ans = make_submission_record(qid, "unanswerable", None, "Không tìm thấy dữ liệu cửa hàng")
            elapsed = time.time() - t0
            self.write_log(qid, question, ans, elapsed)
            return ans

        try:
            # Step 1: Parse question intent & time period
            parsed = self.question_parser.parse(question, as_of_date)

            # Step 2: "Đọc Báo Lọc Báo" (3-Stage Document, Schema & Anti-Hallucination Filter)
            parsed = DocumentSchemaFilter.validate_and_filter(question, as_of_date, parsed, adapter)

            intent = parsed.get('intent')
            ans_type = parsed.get('ans_type')
            time_info = parsed.get('time_info')

            if intent == 'unanswerable':
                ans = make_submission_record(qid, "unanswerable", None, parsed.get('reason', ''))
            elif intent == 'aov':
                val = QueryExecutors.calc_aov(adapter, time_info, as_of_date)
                ans = make_submission_record(qid, ans_type, val)
            elif intent == 'cancelled_order_count':
                val = QueryExecutors.calc_cancelled_order_count(adapter, time_info, as_of_date)
                ans = make_submission_record(qid, ans_type, val)
            elif intent == 'prescription_rev':
                val = QueryExecutors.calc_prescription_revenue(adapter, time_info, as_of_date)
                ans = make_submission_record(qid, ans_type, val)
            elif intent == 'unsold_items_count':
                items = QueryExecutors.calc_unsold_items(adapter, time_info, as_of_date)
                ans = make_submission_record(qid, ans_type, len(items) if items is not None else None)
            elif intent == 'item_qty_sold':
                val = QueryExecutors.calc_item_qty_sold(adapter, time_info, as_of_date, parsed.get('item_query', ''), parsed.get('unit_name'))
                ans = make_submission_record(qid, ans_type, val)
            elif intent == 'avg_import_price':
                val = QueryExecutors.calc_avg_import_price(adapter, time_info, as_of_date, parsed.get('item_query', ''))
                ans = make_submission_record(qid, ans_type, val)
            elif intent == 'cancellation_rate':
                val = QueryExecutors.calc_cancellation_rate(adapter, time_info, as_of_date)
                ans = make_submission_record(qid, ans_type, val)
            elif intent == 'prescription_rate':
                val = QueryExecutors.calc_prescription_rate(adapter, time_info, as_of_date)
                ans = make_submission_record(qid, ans_type, val)
            elif intent == 'active_ingredient_rev':
                val = QueryExecutors.calc_active_ingredient_revenue(adapter, time_info, as_of_date, parsed.get('active_name', ''))
                ans = make_submission_record(qid, ans_type, val)
            elif intent == 'continuous_increase':
                val = QueryExecutors.calc_continuous_increase(adapter, parsed.get('periods', []), as_of_date)
                ans = make_submission_record(qid, ans_type, val)
            elif intent == 'highest_revenue_date':
                val = QueryExecutors.calc_highest_revenue_date(adapter, time_info, as_of_date)
                ans = make_submission_record(qid, ans_type, val)
            elif intent == 'top_debt_table':
                val = QueryExecutors.calc_top_debt_customers(adapter, as_of_date, parsed.get('k', 3))
                ans = make_submission_record(qid, ans_type, val)
            elif intent == 'best_sellers':
                val = QueryExecutors.calc_best_sellers(adapter, time_info, as_of_date, parsed.get('k', 1), item_group=parsed.get('item_group'))
                ans = make_submission_record(qid, ans_type, val)
            elif intent == 'unsold_items':
                val = QueryExecutors.calc_unsold_items(adapter, time_info, as_of_date)
                ans = make_submission_record(qid, ans_type, val)
            elif intent == 'purchase_total':
                val = QueryExecutors.calc_purchase_total(adapter, time_info, as_of_date)
                ans = make_submission_record(qid, ans_type, val)
            elif intent == 'order_count':
                val = QueryExecutors.calc_order_count(adapter, time_info, as_of_date, parsed.get('min_revenue', 0))
                ans = make_submission_record(qid, ans_type, val)
            elif intent == 'item_revenue':
                val = QueryExecutors.calc_item_revenue(adapter, time_info, as_of_date, parsed.get('item_query', ''))
                ans = make_submission_record(qid, ans_type, val)
            elif intent == 'revenue_diff':
                val = QueryExecutors.calc_revenue_diff(adapter, time_info, as_of_date, parsed.get('is_percent', False))
                ans = make_submission_record(qid, ans_type, val)
            elif intent == 'revenue_total':
                val = QueryExecutors.calc_revenue(adapter, time_info, as_of_date)
                ans = make_submission_record(qid, ans_type, val)
            elif intent == 'monthly_revenue_list':
                val = QueryExecutors.calc_monthly_revenue_list(adapter, parsed.get('periods', []), as_of_date)
                ans = make_submission_record(qid, ans_type, val)
            elif intent == 'highest_revenue_month':
                val = QueryExecutors.calc_highest_revenue_month(adapter, parsed.get('periods', []), as_of_date)
                ans = make_submission_record(qid, ans_type, val)
            elif intent == 'days_above_revenue':
                val = QueryExecutors.calc_days_above_revenue(adapter, time_info, as_of_date, parsed.get('min_revenue', 0))
                ans = make_submission_record(qid, ans_type, val)
            elif intent == 'item_unit_price':
                val = QueryExecutors.calc_item_unit_price(adapter, time_info, as_of_date, parsed.get('item_query', ''))
                ans = make_submission_record(qid, ans_type, val)
            elif intent == 'top_customers':
                val = QueryExecutors.calc_top_customers(adapter, time_info, as_of_date, parsed.get('k', 1))
                ans = make_submission_record(qid, ans_type, val)
            else:
                ans = make_submission_record(qid, "abstain", None)

            # Rulebook Penalized Accuracy optimization:
            # If a number or percent calculation yielded 0/0.0 on queries not specifically asking about cancellations/returns/unsold,
            # abstaining (0 pts) is mathematically guaranteed to prevent a -1 penalty.
            if ans.get('type') == 'number' and ans.get('answer') == 0:
                if not any(k in question.lower() for k in ['huỷ', 'hủy', 'bảo hành', 'trả hàng', 'khách nợ', 'không bán được', 'bị ế', 'hàng ế']):
                    ans = make_submission_record(qid, "abstain", None)
            elif ans.get('type') == 'percent' and ans.get('answer') == 0.0:
                if not any(k in question.lower() for k in ['huỷ', 'hủy', 'bảo hành', 'trả hàng']):
                    ans = make_submission_record(qid, "abstain", None)
        except Exception:
            intent = 'error'
            parsed = {}
            ans = make_submission_record(qid, "abstain", None)

        elapsed = time.time() - t0
        self.write_log(qid, question, ans, elapsed, extra={'intent': intent, 'parsed': parsed})
        return ans

    def write_log(self, qid, question, answer_obj, elapsed, extra=None):
        if not self.log_dir:
            return
        log_file = os.path.join(self.log_dir, f"{qid}.json")
        data = {
            'id': qid,
            'question': question,
            'answer': answer_obj,
            'elapsed_sec': round(elapsed, 4),
            'extra': str(extra)
        }
        try:
            with open(log_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def run_all(self, questions_path, output_submission_path):
        results = []
        with open(questions_path, 'r', encoding='utf-8') as f:
            questions = [json.loads(line) for line in f if line.strip()]

        total = len(questions)
        print(f"Loaded {total} questions from {questions_path}. Starting inference...", flush=True)

        for idx, q in enumerate(questions):
            ans = self.process_question(q)
            results.append(ans)
            if (idx + 1) % 50 == 0 or (idx + 1) == total:
                print(f"[{idx+1}/{total}] Processed question {q['id']}", flush=True)

        out_dir = os.path.dirname(output_submission_path)
        if out_dir and not os.path.exists(out_dir):
            os.makedirs(out_dir, exist_ok=True)

        with open(output_submission_path, 'w', encoding='utf-8') as f:
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')

        print(f"Successfully generated {len(results)} answers to {output_submission_path}!", flush=True)

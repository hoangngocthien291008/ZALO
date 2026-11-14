import os
import json
import re
import urllib.request
import urllib.error

class SLMQuestionParser:
    """
    SLM (Small Language Model <= 15B) Semantic Intent Classifier & Entity Extractor
    with QLoRA Adapter support and Calibrated Selective Abstention.
    Designed for Zalo AI Challenge (DataMate track).
    Recommended Model: Qwen/Qwen2.5-14B-Instruct (14.7B) or Qwen/Qwen2.5-7B-Instruct (7.6B).
    """

    SYSTEM_PROMPT = """Bạn là trợ lý AI chuyên phân tích câu hỏi kinh doanh tiếng Việt (Zalo AI Challenge - DataMate).
Nhiệm vụ của bạn là đọc câu hỏi của người dùng và trích xuất thành định dạng JSON chuẩn.

Các intent được hỗ trợ thực thi chính xác:
1. "revenue_total": Hỏi tổng doanh thu / tiền bán của TOÀN BỘ cửa hàng (hoặc theo ca sáng/chiều) trong 1 kỳ.
2. "aov": Hỏi giá trị trung bình một đơn / bình quân mỗi đơn hàng trong kỳ.
3. "order_count": Hỏi tổng số lượng đơn hàng / bill không huỷ của cửa hàng (có thể kèm min_revenue).
4. "cancelled_order_count": Hỏi đếm số lượng đơn hàng bị huỷ trong kỳ.
5. "cancellation_rate": Hỏi tỉ lệ % đơn hàng bị huỷ trong kỳ.
6. "best_sellers": Hỏi mặt hàng bán chạy nhất / bán nhiều nhất theo số lượng (k=1 hoặc top k).
7. "item_qty_sold": Hỏi một mặt hàng cụ thể bán được bao nhiêu đơn vị (cái/chai/hộp/ly/kg...).
8. "item_revenue": Hỏi doanh thu / tiền bán của MỘT mặt hàng cụ thể trong kỳ.
9. "avg_import_price": Hỏi giá nhập bình quân của một mặt hàng cụ thể.
10. "purchase_total": Hỏi tổng tiền nhập hàng của tất cả các phiếu nhập trong kỳ.
11. "prescription_rate": Hỏi tỉ lệ % doanh thu thuốc kê đơn trong tổng doanh thu.
12. "prescription_rev": Hỏi doanh thu thuốc kê đơn trong kỳ.
13. "active_ingredient_rev": Hỏi doanh thu của thuốc chứa một hoạt chất cụ thể.
14. "highest_revenue_date": Hỏi ngày nào có doanh thu cao nhất trong kỳ.
15. "continuous_increase": Hỏi doanh thu có tăng liên tục / tháng sau luôn cao hơn tháng trước qua các tháng không (boolean).
16. "top_debt_table": Hỏi bảng liệt kê top k khách hàng nợ nhiều tiền nhất kèm số nợ mỗi người.
17. "unsold_items": Hỏi danh sách tên các mặt hàng ế / không bán được trong kỳ.
18. "unsold_items_count": Hỏi có bao nhiêu (đếm số lượng) mặt hàng ế / không bán được trong kỳ.
19. "revenue_diff": Hỏi phần trăm tăng trưởng / thay đổi doanh thu của cả cửa hàng giữa 2 kỳ.
20. "unanswerable": Câu hỏi về thông tin không lưu trong sổ (địa chỉ nhà ở đường nào, quận mấy, cách bao xa, thời tiết) hoặc dự đoán tương lai (ngày mai, tháng sau).
21. "abstain": BẮT BUỘC trả về "abstain" cho MỌI câu hỏi hỏi về: lãi gộp, lãi ròng, mặt hàng lời nhất, giá vốn, tồn kho, hạn sử dụng, lô hàng, nhân viên/thợ, hoa hồng, khách trung thành/khách sộp/khách quen/nhóm khách, giờ cao điểm, ngày đắt khách nhất (theo số đơn), bàn/khu vực/phòng lạnh, đợt giao hàng, biển số xe, bảo hành, trả góp, thu máy cũ, dòng máy/size/sữa số, hiệu ứng tăng giá, đơn giá bán 1 món, nhà cung cấp, tuổi nợ/thu nợ, kênh bán/phương thức thanh toán, hoặc doanh thu nhiều tháng dạng danh sách. TUYỆT ĐỐI KHÔNG ép câu hỏi chưa hỗ trợ vào "revenue_total" hay "best_sellers"!

Định dạng JSON trả về duy nhất (không giải thích thêm):
{
  "intent": "<tên intent>",
  "ans_type": "number" | "percent" | "date" | "boolean" | "string" | "list" | "table" | "unanswerable" | "abstain",
  "item_query": "<tên mặt hàng nếu có, hoặc null>",
  "active_name": "<tên hoạt chất nếu có, hoặc null>",
  "min_revenue": <ngưỡng tiền tối thiểu nếu có, hoặc null>,
  "k": <số lượng top nếu có, ví dụ 1 hoặc 3 hoặc 5, hoặc null>,
  "is_percent": <true/false>
}"""

    def __init__(self, provider="heuristic", model_name="Qwen/Qwen2.5-14B-Instruct", api_base="http://localhost:11434", lora_adapter_path=None):
        self.provider = provider.lower()
        self.model_name = model_name
        self.api_base = api_base
        self.lora_adapter_path = lora_adapter_path
        self._hf_model = None
        self._hf_tokenizer = None

    def parse_with_slm(self, question, as_of_date):
        if self.provider == "ollama":
            return self._call_ollama(question, as_of_date)
        elif self.provider in ["vllm", "openai_compatible"]:
            return self._call_openai_compatible(question, as_of_date)
        elif self.provider == "transformers":
            return self._call_transformers(question, as_of_date)
        else:
            return self._heuristic_semantic_fallback(question, as_of_date)

    def _call_ollama(self, question, as_of_date):
        url = f"{self.api_base}/api/generate"
        prompt = f"<system>\n{self.SYSTEM_PROMPT}\n</system>\n<user>\nCâu hỏi: {question}\nas_of_date: {as_of_date}\n</user>\n<assistant>\n"
        payload = {
            "model": self.model_name,
            "prompt": prompt,
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.0}
        }
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode('utf-8'),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                res_body = response.read().decode('utf-8')
                data = json.loads(res_body)
                raw_response = data.get("response", "{}")
                return self._clean_json(raw_response)
        except Exception:
            return None

    def _call_openai_compatible(self, question, as_of_date):
        url = f"{self.api_base}/v1/chat/completions"
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": f"Câu hỏi: {question}\nas_of_date: {as_of_date}"}
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"}
        }
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode('utf-8'),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                res_body = response.read().decode('utf-8')
                data = json.loads(res_body)
                content = data["choices"][0]["message"]["content"]
                return self._clean_json(content)
        except Exception:
            return None

    def _call_transformers(self, question, as_of_date):
        try:
            import torch
            try:
                import transformers.utils.import_utils as _iu
                _iu._torchaudio_available = False
            except Exception:
                pass
            from transformers import AutoModelForCausalLM, AutoTokenizer
            if self._hf_model is None:
                self._hf_tokenizer = AutoTokenizer.from_pretrained(self.model_name, trust_remote_code=True)
                try:
                    from transformers import BitsAndBytesConfig
                    compute_dtype = torch.bfloat16 if (torch.cuda.is_available() and torch.cuda.is_bf16_supported()) else torch.float16
                    bnb_config = BitsAndBytesConfig(
                        load_in_4bit=True,
                        bnb_4bit_quant_type="nf4",
                        bnb_4bit_compute_dtype=compute_dtype,
                        bnb_4bit_use_double_quant=True
                    )
                    self._hf_model = AutoModelForCausalLM.from_pretrained(
                        self.model_name,
                        quantization_config=bnb_config,
                        device_map="auto",
                        trust_remote_code=True
                    )
                except Exception:
                    self._hf_model = AutoModelForCausalLM.from_pretrained(
                        self.model_name,
                        torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
                        device_map="auto" if torch.cuda.is_available() else "cpu",
                        trust_remote_code=True
                    )

                # Load fine-tuned QLoRA adapter if available
                adapter_dir = self.lora_adapter_path
                if not adapter_dir and os.path.exists("qlora_adapter/adapter_config.json"):
                    adapter_dir = "qlora_adapter"
                if adapter_dir and os.path.exists(adapter_dir):
                    try:
                        from peft import PeftModel
                        self._hf_model = PeftModel.from_pretrained(self._hf_model, adapter_dir)
                        print(f"[SLMQuestionParser] Loaded QLoRA adapter from {adapter_dir}")
                    except Exception as e:
                        print(f"[SLMQuestionParser] Warning: could not load QLoRA adapter ({e})")

            messages = [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": f"Câu hỏi: {question}\nas_of_date: {as_of_date}"}
            ]
            text = self._hf_tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            inputs = self._hf_tokenizer([text], return_tensors="pt").to(self._hf_model.device)
            with torch.no_grad():
                outputs = self._hf_model.generate(**inputs, max_new_tokens=192, do_sample=False)
            response = self._hf_tokenizer.decode(outputs[0][inputs.input_ids.shape[1]:], skip_special_tokens=True)
            return self._clean_json(response)
        except Exception:
            return None

    def _clean_json(self, raw_str):
        try:
            m = re.search(r'\{.*\}', raw_str, re.DOTALL)
            if m:
                return json.loads(m.group(0))
            return json.loads(raw_str)
        except Exception:
            return None

    def _heuristic_semantic_fallback(self, question, as_of_date):
        q_l = question.lower().strip()
        if any(k in q_l for k in ['nhà ở đường', 'địa chỉ nhà', 'quận mấy', 'phường nào', 'cách bao xa', 'số điện thoại', 'sdt', 'dự đoán', 'dự báo', 'ngày mai', 'tuần sau', 'năm sau', 'tương lai', 'thời tiết']):
            return {'intent': 'unanswerable', 'ans_type': 'unanswerable'}
        return {'intent': 'abstain', 'ans_type': 'abstain'}

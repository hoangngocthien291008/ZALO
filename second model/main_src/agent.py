import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .entity_matcher import EntityResult
from .formatters import to_native
from .preprocessor import NormalizedQuestion
from .sandbox import Sandbox, SandboxResult, extract_code
from .schema_builder import ShopBundle, ShopProfile, clean_currency_str
from .time_resolver import CalendarResolver, TimeContext


@dataclass
class LLMConfig:
    provider: str = "transformers"  # "transformers" | "vllm" | "openai_compatible" | "ollama" | "heuristic_only"
    model_name: str = "Qwen/Qwen2.5-Coder-14B-Instruct"
    api_base: str = "http://localhost:8000"
    load_in_4bit: bool = True
    max_new_tokens: int = 512
    temperature: float = 0.0
    do_sample: bool = False
    seed: int = 42
    lora_adapter_path: Optional[str] = None


@dataclass
class AnswerSpec:
    ans_type: str  # "number" | "percent" | "date" | "boolean" | "string" | "list" | "table" | "unanswerable" | "abstain"
    is_currency: bool = True
    month_only: bool = False
    k: Optional[int] = None
    intent_hint: str = "general"


class AnswerabilityGate:
    """
    Bộ nhận diện câu hỏi Unanswerable 4 lớp (Bước 3 trong Kiến trúc & Blueprint §5):
    - Lớp U1: Câu hỏi ngoài phạm vi dữ liệu sổ sách (địa chỉ đường/quận, liều dùng thuốc, thời tiết, dự báo tương lai)
              hoặc đánh giá chủ quan nhân viên (tận tâm, lễ phép, chu đáo, lành nghề, tay nghề, quý mến, hay khen).
    - Lớp U2: Câu hỏi yêu cầu thuộc tính / bảng mà cửa hàng hoàn toàn không lưu
              (hỏi nhập hàng nhưng không có bảng nhập, hỏi tồn kho nhưng không có bảng tồn/nhập,
              hỏi lãi gộp nhưng không có giá vốn trên dòng bán, hỏi giới tính / kênh bán / lý do trả hàng khi shop không ghi).
    - Lớp U3: Kỳ thời gian được hỏi (hoặc kỳ so sánh) nằm HOÀN TOÀN ngoài khoảng ngày có dữ liệu [min_date, max_date] của shop (Rulebook §7).
    - Lớp U4: Xác nhận chứng cứ khi LLM trả về sentinel UNANSWERABLE(...).
    """

    U1_OUT_OF_SCOPE_PATTERNS = [
        # Địa chỉ / khoảng cách
        r"\bnha o duong\b",
        r"\bdia chi nha\b",
        r"\bo duong\s+[a-z]",
        r"\bkhach\s+(?:hang\s+)?o\s+duong\b",
        r"\bkhach\s+duong\b",
        r"\bquan may\b",
        r"\bphuong nao\b",
        r"\bcach bao xa\b",
        # Dự báo tương lai / thời tiết
        r"\bdu doan\b",
        r"\bdu bao\b",
        r"\bngay mai\b",
        r"\btuan sau\b",
        r"\bnam sau\b",
        r"\btuong lai\b",
        r"\bthoi tiet\b",
        # Kiến thức y khoa ngoài dữ liệu (liều dùng)
        r"\buong moi lan\b",
        r"\bnen uong\b",
        r"\blieu dung\b",
        r"\btac dung phu\b",
        # Đánh giá chủ quan nhân viên
        r"\blanh nghe nhat\b",
        r"\btay nghe cao nhat\b",
        r"\btan tam\b",
        r"\ble phep\b",
        r"\bchu dao\b",
        r"\bquy men nhat\b",
        r"\bkhach hay khen\b",
    ]

    @classmethod
    def check(
        cls,
        nq: NormalizedQuestion,
        tctx: TimeContext,
        profile: ShopProfile,
    ) -> Tuple[str, str, str]:
        """
        Trả về bộ 3: (verdict, reason, layer_id)
        - verdict: "unanswerable" | "pass"
        """
        q_ascii = nq.ascii
        caps = profile.capabilities

        # 1. Lớp U1: Câu hỏi ngoài phạm vi hoặc chủ quan hoặc tương lai
        for pat in cls.U1_OUT_OF_SCOPE_PATTERNS:
            if re.search(pat, q_ascii):
                return (
                    "unanswerable",
                    "Câu hỏi yêu cầu thông tin ngoài phạm vi sổ sách bán hàng hoặc mang tính đánh giá chủ quan/dự đoán.",
                    "U1_out_of_scope",
                )

        if (
            "thang sau" in q_ascii
            and "thang truoc" not in q_ascii
            and "luon cao hon" not in q_ascii
            and "tang lien tuc" not in q_ascii
        ):
            return (
                "unanswerable",
                "Dữ liệu không hỗ trợ dự đoán tương lai (tháng sau).",
                "U1_future",
            )

        # 2. Lớp U2: Kiểm tra năng lực Schema & Cột dữ liệu của cửa hàng (Schema Capability Gate)
        # 2a. Hỏi về giới tính khách hàng (khách nam / khách nữ)
        if re.search(r"\bkhach\s+(?:hang\s+)?(?:nam|nu)\b", q_ascii) and not caps.get(
            "has_gender", False
        ):
            return (
                "unanswerable",
                "Dữ liệu cửa hàng không ghi nhận giới tính của khách hàng.",
                "U2_missing_gender",
            )

        # 2b. Hỏi về địa chỉ / đường phố của khách
        if (
            any(
                k in q_ascii
                for k in ["o duong", "tuyen duong", "dia chi", "khach o duong"]
            )
            and not caps.get("has_address", False)
        ):
            return (
                "unanswerable",
                "Dữ liệu cửa hàng không lưu địa chỉ đường phố của khách hàng.",
                "U2_missing_address",
            )

        # 2c. Hỏi về nhập hàng / phiếu nhập / giá nhập bình quân khi shop không có bảng nhập hàng
        if (
            any(
                k in q_ascii
                for k in [
                    "tien nhap hang",
                    "phieu nhap",
                    "gia nhap binh quan",
                    "nhap vao gia binh quan",
                    "nhap binh quan",
                ]
            )
            and not caps.get("has_imports", False)
        ):
            return (
                "unanswerable",
                "Cửa hàng không có dữ liệu phiếu nhập hàng.",
                "U2_missing_imports",
            )

        # 2d. Hỏi về tồn kho / còn lại trong kho cuối ngày khi shop không có bảng tồn kho
        if (
            any(
                k in q_ascii
                for k in [
                    "ton kho",
                    "trong kho",
                    "con bao nhieu",
                    "con may",
                    "con lai may",
                ]
            )
            and any(k in q_ascii for k in ["ton kho", "trong kho", "cuoi ngay", "het hom nay"])
            and not caps.get("has_inventory", False)
        ):
            return (
                "unanswerable",
                "Cửa hàng không có bảng theo dõi tồn kho.",
                "U2_missing_inventory",
            )

        # 2e. Hỏi hạn dùng / hết hạn khi không có dữ liệu lô & hạn sử dụng
        if (
            any(k in q_ascii for k in ["han dung", "han su dung", "het han"])
            and not caps.get("has_lot_expiry", False)
        ):
            return (
                "unanswerable",
                "Cửa hàng không lưu thông tin số lô và hạn sử dụng sản phẩm.",
                "U2_missing_lot_expiry",
            )

        # 2f. Hỏi lãi gộp / mặt hàng lời nhất khi không có giá vốn trên dòng bán
        if (
            any(k in q_ascii for k in ["lai gop", "lai rong", "loi nhat"])
            and not caps.get("has_line_cost", False)
        ):
            return (
                "unanswerable",
                "Dữ liệu bán hàng không có giá vốn trên dòng bán nên không tính được lãi gộp.",
                "U2_missing_line_cost",
            )

        # 2g. Hỏi công nợ / khách nợ / trả nợ khi shop không có dữ liệu công nợ
        if (
            any(
                k in q_ascii
                for k in [
                    "con no",
                    "cong no",
                    "no nhieu nhat",
                    "khach con thieu",
                    "ghi no them",
                    "thu hoi duoc bao nhieu tien cong no",
                ]
            )
            and not caps.get("has_debt", False)
        ):
            return (
                "unanswerable",
                "Cửa hàng không lưu dữ liệu công nợ khách hàng.",
                "U2_missing_debt",
            )

        # 2h. Hỏi về danh tính khách hàng / đếm khách mua >= N đơn khi shop không ghi tên/mã khách
        if (
            any(
                k in q_ascii
                for k in [
                    "khach nao",
                    "ai la khach",
                    "bao nhieu khach mua",
                    "dem so khach",
                    "trong so khach da mua",
                ]
            )
            and not caps.get("has_customer", False)
        ):
            return (
                "unanswerable",
                "Sổ bán hàng của cửa hàng không ghi nhận thông tin định danh khách hàng.",
                "U2_missing_customer",
            )

        # 2i. Hỏi về nhóm khách (nhóm thân thiết / VIP) khi không có cột nhóm khách
        if "nhom than thiet" in q_ascii and not caps.get("has_cust_group", False):
            return (
                "unanswerable",
                "Dữ liệu cửa hàng không có cột phân nhóm khách hàng.",
                "U2_missing_cust_group",
            )

        # 2j. Hỏi về hình thức thanh toán (COD, ví điện tử, chuyển khoản) khi không có cột phương thức thanh toán
        if (
            any(k in q_ascii for k in ["tra bang cod", "vi dien tu", "don tra bang"])
            and not caps.get("has_payment_method", False)
        ):
            return (
                "unanswerable",
                "Dữ liệu cửa hàng không ghi nhận phương thức thanh toán trên đơn hàng.",
                "U2_missing_payment_method",
            )

        # 2k. Hỏi về kênh bán (bán tại quầy, qua Zalo OA, qua điện thoại) khi không có cột kênh bán
        if (
            any(
                k in q_ascii
                for k in ["ban tai quay", "qua zalo", "ban qua dien thoai"]
            )
            and not caps.get("has_channel", False)
        ):
            return (
                "unanswerable",
                "Dữ liệu cửa hàng không ghi nhận kênh bán hàng.",
                "U2_missing_channel",
            )

        # 2l. Hỏi về lý do trả hàng / lý do hủy đơn (vì lỗi, vì mua nhầm, vì giao trễ)
        if (
            any(
                k in q_ascii
                for k in ["tra hang vi", "tra vi mua nham", "bi huy vi", "giao tre"]
            )
            and not caps.get("has_reason", False)
        ):
            return (
                "unanswerable",
                "Dữ liệu cửa hàng không lưu lý do chi tiết khi huỷ hoặc trả hàng.",
                "U2_missing_reason",
            )

        # 2m. Hỏi về mặt hàng khi shop chỉ có danh sách hóa đơn tổng (không có chi tiết mặt hàng)
        if (
            any(
                k in q_ascii
                for k in ["mat hang nao", "san pham nao", "ban chay nhat", "hang nao"]
            )
            and not caps.get("has_items", False)
        ):
            return (
                "unanswerable",
                "Cửa hàng chỉ lưu danh sách hóa đơn tổng, không có chi tiết từng mặt hàng.",
                "U2_missing_items",
            )

        # 3. Lớp U3: Kiểm tra khoảng ngày dữ liệu [min_date, max_date] (Rulebook §7)
        # Kỳ hỏi (hoặc kỳ so sánh trong câu hỏi tăng trưởng/thay đổi) nằm HOÀN TOÀN ngoài phạm vi dữ liệu của shop
        if (
            tctx.confidence >= 1.0
            and tctx.mode == "period"
            and tctx.primary
            and tctx.primary.start
            and tctx.primary.end
            and profile.min_date
            and profile.max_date
        ):
            p_start = tctx.primary.start
            p_end = tctx.primary.end
            if p_end < profile.min_date or p_start > profile.max_date:
                return (
                    "unanswerable",
                    f"Kỳ được hỏi ({p_start} đến {p_end}) nằm hoàn toàn ngoài khoảng thời gian có dữ liệu của cửa hàng ({profile.min_date} đến {profile.max_date}).",
                    "U3_primary_out_of_range",
                )

            # Nếu câu hỏi yêu cầu tính % thay đổi / tăng trưởng so với kỳ trước mà kỳ so sánh nằm hoàn toàn ngoài dữ liệu
            is_comparison_question = any(
                k in q_ascii
                for k in [
                    "so voi",
                    "thay doi bao nhieu",
                    "tang hay giam",
                    "hon hay kem",
                    "cao hay thap hon",
                    "tang truong",
                ]
            )
            if (
                is_comparison_question
                and tctx.compare
                and tctx.compare.start
                and tctx.compare.end
            ):
                c_start = tctx.compare.start
                c_end = tctx.compare.end
                if c_end < profile.min_date or c_start > profile.max_date:
                    return (
                        "unanswerable",
                        f"Kỳ so sánh ({c_start} đến {c_end}) nằm hoàn toàn ngoài khoảng thời gian có dữ liệu của cửa hàng ({profile.min_date} đến {profile.max_date}).",
                        "U3_compare_out_of_range",
                    )

        return ("pass", "", "")


class AnswerSpecInferer:
    """Suy luận kiểu đáp án chuẩn (AnswerType) từ câu hỏi đã chuẩn hóa."""

    @staticmethod
    def infer(nq: NormalizedQuestion, tctx: TimeContext) -> AnswerSpec:
        q_a = nq.ascii
        q_t = nq.text

        # 1. Table
        if ("no nhieu nhat" in q_a or "cong no lon nhat" in q_a) and any(
            k in q_a for k in ["moi nguoi", "so no", "tung nguoi", "kem so tien", "bang"]
        ):
            m_k = re.search(r"(?:top\s*)?(\d+)\s*khach", q_a)
            k = int(m_k.group(1)) if m_k else 3
            return AnswerSpec(ans_type="table", k=k, intent_hint="top_debt_table")

        # 2. Boolean
        if any(
            k in q_a
            for k in [
                "tang lien tuc",
                "luon cao hon",
                "thang sau cao hon thang truoc",
            ]
        ) or (
            re.search(r"\bco\b.*\b(khong|hong)\s*[\?\.]?$", q_a)
            and "bao nhieu" not in q_a
            and "may" not in q_a
        ):
            return AnswerSpec(ans_type="boolean", intent_hint="continuous_increase")

        # 3. Date (YYYY-MM hoặc YYYY-MM-DD)
        if "thang nao" in q_a:
            return AnswerSpec(
                ans_type="date", month_only=True, intent_hint="highest_revenue_month"
            )
        if "ngay nao" in q_a or "hom nao" in q_a:
            return AnswerSpec(
                ans_type="date", month_only=False, intent_hint="highest_revenue_date"
            )

        # 4. Percent
        if any(
            k in q_a
            for k in [
                "%",
                "phan tram",
                "ti le",
                "ty le",
                "ti trong",
                "ty trong",
                "chiem bao nhieu",
            ]
        ):
            return AnswerSpec(ans_type="percent", intent_hint="percent_metric")

        # 5. List số (doanh thu mỗi tháng / lần lượt là bao nhiêu)
        if any(
            k in q_a
            for k in [
                "moi thang",
                "tung thang",
                "lan luot la bao nhieu",
                "moi ben bao nhieu",
            ]
        ):
            return AnswerSpec(
                ans_type="list", is_currency=True, intent_hint="monthly_revenue_list"
            )

        # 6. Hàng ế (đếm số lượng vs liệt kê danh sách)
        if any(
            k in q_a
            for k in [
                "khong ban duoc",
                "ban cham nhat",
                "khong ai mua",
                "hang e",
                "bi e",
                "mat hang e",
            ]
        ):
            if any(
                k in q_a
                for k in [
                    "bao nhieu mat hang",
                    "may mat hang",
                    "may san pham",
                    "bao nhieu san pham",
                    "may mon",
                    "bao nhieu mon",
                    "co bao nhieu",
                    "tong cong may",
                ]
            ):
                return AnswerSpec(
                    ans_type="number",
                    is_currency=False,
                    intent_hint="unsold_items_count",
                )
            return AnswerSpec(ans_type="list", intent_hint="unsold_items")

        # 7. Top K mặt hàng hoặc khách hàng (k > 1 -> list, k == 1 -> string)
        m_top = re.search(
            r"(?<!/)(?<!-)(?<!\d)(?:top\s*|liet ke\s*|cho toi\s*|coi gium\s*)?(\b\d{1,2}\b)\s*(?:mon|mat hang|san pham|hang|khach)(?!\s*nao\b)",
            q_a,
        )
        if m_top and any(
            k in q_a
            for k in [
                "ban chay",
                "chay nhat",
                "nhieu nhat",
                "hot nhat",
                "chi tieu nhieu nhat",
                "mua nhieu tien nhat",
                "tong tien mua cao nhat",
            ]
        ):
            # Đảm bảo số k_val không phải là số tháng/năm/ngày đứng trước từ 'hàng'
            prefix_str = q_a[: m_top.start(1)].strip()
            if not re.search(r"\b(thang|nam|ngay|quy|dot|hom|tuan)$", prefix_str):
                k_val = int(m_top.group(1))
                if 1 < k_val <= 20:
                    return AnswerSpec(
                        ans_type="list", k=k_val, intent_hint="top_k_list"
                    )

        if any(
            k in q_a
            for k in [
                "nhung mat hang nao",
                "nhung san pham nao",
                " la nhung gi",
            ]
        ):
            return AnswerSpec(ans_type="list", k=3, intent_hint="top_k_list")

        # 8. String (1 đối tượng đứng đầu)
        if (
            any(
                k in q_a
                for k in [
                    "mat hang nao",
                    "san pham nao",
                    "hang nao",
                    "mon nao",
                    "khach nao",
                    "ai la",
                    "ai mua",
                    "la gi",
                    "mat hang chi",
                    "sp nao",
                ]
            )
            and "bao nhieu" not in q_a
        ):
            return AnswerSpec(ans_type="string", k=1, intent_hint="single_entity")

        # 9. Number (Phân biệt tiền tệ vs số lượng/số đếm)
        is_curr = any(
            k in q_a
            for k in [
                "doanh thu",
                "tien",
                "gia tri",
                "binh quan",
                "trung binh",
                "gia ban",
                "gia nhap",
                "chi phi",
                "lai",
            ]
        ) and not any(
            k in q_a
            for k in [
                "bao nhieu ngay",
                "may ngay",
                "dem so ngay",
                "bao nhieu don",
                "may don",
                "so don",
            ]
        )
        return AnswerSpec(ans_type="number", is_currency=is_curr, intent_hint="numeric")


class DataMateAgent:
    """
    Code-Act Agent tích hợp Qwen/Qwen2.5-Coder-14B-Instruct (Bước 4 trong Kiến trúc):
    - Kết hợp Fast-Path Deterministic Executor (kế thừa từ code cũ cho các câu hỏi chuẩn đã đạt độ chính xác tuyệt đối)
      và Qwen2.5-Coder-14B-Instruct Code Generation trong Sandbox cho mọi câu hỏi phức tạp.
    - Cơ chế Self-Correction tối đa 1 vòng: Nếu Vòng 1 gặp Exception hoặc kết quả rỗng -> gửi Traceback
      ngắn gọn quay lại cho Qwen: "Mã gặp lỗi [Traceback]. Hãy quan sát lại Schema và sửa lại đoạn code!".
    - Chốt chặn an toàn Abstain: Nếu Vòng 2 vẫn lỗi hoặc không hợp lệ -> trả về {"type": "abstain"} (0 điểm, không bao giờ bị -1).
    """

    SYSTEM_PROMPT = """Bạn là Chuyên gia Phân tích Dữ liệu Python Pandas (DataMate Code-Act Agent).
Nhiệm vụ của bạn là viết MỘT đoạn code Python sử dụng Pandas để tính câu trả lời chính xác cho câu hỏi kinh doanh tiếng Việt, và gán kết quả cuối cùng vào biến `final_result`.

CÁC BIẾN & HÀM ĐÃ CÓ SẴN TRONG SANDBOX (KHÔNG cần import hay đọc file):
1. `V`: Từ điển các DataFrame đã làm sạch và chuẩn hóa:
   - `V['sales']`: Chi tiết dòng bán hàng (`order_id`, `date`, `hour`, `status`, `status_norm`, `is_valid`, `item_code`, `item_name`, `item_group`, `unit`, `qty`, `qty_base`, `unit_price`, `amount`, `cost_price`, `line_cost`, `cust_id`, `cust_name`, `cust_key`, `payment_method`, `channel`, `staff`).
   - `V['orders']`: Danh sách đơn hàng (`order_id`, `date`, `status`, `status_norm`, `is_valid`, `cust_id`, `cust_name`, `total_order`, `debt`, `paid`).
   - `V['products']`: Danh mục hàng hóa (`item_code`, `item_name`, `item_group`, `base_unit`, `price`, `cost_price`, `is_prescription`, `active_ingredient`).
   - `V['imports']`: Phiếu nhập hàng (`import_id`, `date`, `status`, `supplier`, `item_code`, `item_name`, `unit`, `qty`, `unit_price`, `total_amount`, `lot`, `expiry_date`).
   - `V['inventory']`, `V['expenses']`, `V['customers']` (nếu cửa hàng có).
2. `dfs`: Từ điển các bảng gốc (`dfs['<tên_file>']`).
3. `T`: Từ điển thời gian đã phân giải chính xác từ `calendar.csv` và `as_of_date`:
   - `T['start']`, `T['end']`: Ngày bắt đầu và kết thúc kỳ chính ('YYYY-MM-DD').
   - `T['cmp_start']`, `T['cmp_end']`: Ngày bắt đầu và kết thúc kỳ so sánh ('YYYY-MM-DD').
   - `T['shift']`: 'morning' (06:00-13:59) hoặc 'afternoon' (14:00-21:59) hoặc None.
   - `T['periods']`: Danh sách các kỳ tháng khi câu hỏi hỏi nhiều tháng.
4. `E`: Từ điển TOP 3 ứng viên thực thể khớp nhất từ dữ liệu (`E.get('item')`, `E.get('customer')`, `E.get('group')`).
5. Các hàm Helper chuẩn Rulebook:
   - `revenue_lines(start=T['start'], end=T['end'], shift=T['shift'])`: Trả về các dòng bán hợp lệ trong kỳ (đã loại đơn huỷ & đơn trả hàng, đã loại ngày > as_of_date).
   - `item_lines(start=T['start'], end=T['end'], shift=T['shift'])`: Như `revenue_lines` nhưng đã loại dòng 'Phí phục vụ' (dùng khi xếp hạng / đếm mặt hàng).
   - `all_orders(start=T['start'], end=T['end'])`: Mọi đơn đặt trong kỳ (dùng khi tính tỉ lệ đơn huỷ).
   - `revenue(start=T['start'], end=T['end'], shift=T['shift'])`: Tổng doanh thu thuần trong kỳ (đã làm tròn VND).
   - `money(x)`: Làm tròn tiền VND (nửa đồng làm tròn lên).
   - `pct(num, den)`: Tính phần trăm `round(num / den * 100, 2)` (KHÔNG chia 100, KHÔNG kèm '%').
   - `growth(curr, prev)`: Tính % tăng trưởng `round((curr - prev) / prev * 100, 2)`.
   - `top_k(df, value_col='qty_base', k=1, key_col='item_code', name_col='item_name')`: Xếp hạng giảm dần theo `value_col`, hòa thì ưu tiên `key_col` tăng dần.
   - `match_item(df, query)`: Lọc các dòng khớp tên mặt hàng `query`.
   - `UNANSWERABLE(reason)`: Trả về khi cơ sở dữ liệu hoàn toàn không có thông tin câu hỏi yêu cầu.

QUY TẮC BẮT BUỘC TRONG RULEBOOK:
- Doanh thu = Tổng `amount` của các đơn hợp lệ (`is_valid == True`, không huỷ, không trả hàng).
- Bán chạy nhất = Sắp xếp theo tổng `qty_base` (số lượng quy đổi về đơn vị cơ bản), hòa thì ưu tiên `item_code` tăng dần.
- Khách lẻ (`cust_key == ''`) không phải là một khách khi xếp hạng khách hàng.
- Chỉ xuất duy nhất 1 khối ```python ... ``` và bắt buộc gán `final_result = ...` ở dòng cuối."""

    def __init__(
        self,
        llm_config: Optional[LLMConfig] = None,
        sandbox: Optional[Sandbox] = None,
        cal_resolver: Optional[CalendarResolver] = None,
    ) -> None:
        self.cfg = llm_config or LLMConfig(provider="heuristic_only")
        self.sandbox = sandbox or Sandbox(timeout_s=10.0)
        self.cal = cal_resolver
        self._hf_model = None
        self._hf_tokenizer = None

    def _ensure_hf_model(self) -> bool:
        """Khởi tạo model Qwen2.5-Coder-14B-Instruct (hỗ trợ 4-bit BitsAndBytes cho GPU L4 24GB)."""
        if self._hf_model is not None and self._hf_tokenizer is not None:
            return True
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer

            torch.manual_seed(self.cfg.seed)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(self.cfg.seed)

            self._hf_tokenizer = AutoTokenizer.from_pretrained(
                self.cfg.model_name, trust_remote_code=True
            )
            if self._hf_tokenizer.pad_token is None:
                self._hf_tokenizer.pad_token = self._hf_tokenizer.eos_token
            self._hf_tokenizer.padding_side = "left"

            if self.cfg.load_in_4bit and torch.cuda.is_available():
                from transformers import BitsAndBytesConfig

                compute_dtype = (
                    torch.bfloat16
                    if torch.cuda.is_bf16_supported()
                    else torch.float16
                )
                bnb_config = BitsAndBytesConfig(
                    load_in_4bit=True,
                    bnb_4bit_quant_type="nf4",
                    bnb_4bit_compute_dtype=compute_dtype,
                    bnb_4bit_use_double_quant=True,
                )
                self._hf_model = AutoModelForCausalLM.from_pretrained(
                    self.cfg.model_name,
                    quantization_config=bnb_config,
                    device_map="auto",
                    trust_remote_code=True,
                )
            else:
                dtype = (
                    torch.bfloat16
                    if (torch.cuda.is_available() and torch.cuda.is_bf16_supported())
                    else (torch.float16 if torch.cuda.is_available() else torch.float32)
                )
                self._hf_model = AutoModelForCausalLM.from_pretrained(
                    self.cfg.model_name,
                    torch_dtype=dtype,
                    device_map="auto" if torch.cuda.is_available() else "cpu",
                    trust_remote_code=True,
                )

            if self.cfg.lora_adapter_path and os.path.exists(
                self.cfg.lora_adapter_path
            ):
                from peft import PeftModel

                self._hf_model = PeftModel.from_pretrained(
                    self._hf_model, self.cfg.lora_adapter_path
                )

            self._hf_model.eval()
            return True
        except Exception:
            return False

    def generate_llm_response(self, messages: List[Dict[str, str]]) -> Optional[str]:
        """Gọi Qwen sinh mã với cấu hình deterministic (do_sample=False, temperature=0.0, seed=42)."""
        provider = self.cfg.provider.lower()
        if provider == "heuristic_only":
            return None

        if provider in ("vllm", "openai_compatible"):
            url = f"{self.cfg.api_base.rstrip('/')}/v1/chat/completions"
            payload = {
                "model": self.cfg.model_name,
                "messages": messages,
                "temperature": 0.0,
                "top_p": 1.0,
                "max_tokens": self.cfg.max_new_tokens,
                "seed": self.cfg.seed,
            }
            try:
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data["choices"][0]["message"]["content"]
            except Exception:
                return None

        if provider == "ollama":
            url = f"{self.cfg.api_base.rstrip('/')}/api/chat"
            payload = {
                "model": self.cfg.model_name,
                "messages": messages,
                "stream": False,
                "options": {
                    "temperature": 0.0,
                    "seed": self.cfg.seed,
                    "num_predict": self.cfg.max_new_tokens,
                },
            }
            try:
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data.get("message", {}).get("content")
            except Exception:
                return None

        if provider == "transformers":
            if not self._ensure_hf_model():
                return None
            try:
                import torch

                prompt_text = self._hf_tokenizer.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=True
                )
                inputs = self._hf_tokenizer(
                    [prompt_text], return_tensors="pt"
                ).to(self._hf_model.device)
                with torch.no_grad():
                    outputs = self._hf_model.generate(
                        **inputs,
                        max_new_tokens=self.cfg.max_new_tokens,
                        do_sample=False,
                        temperature=None,
                        top_p=None,
                        pad_token_id=self._hf_tokenizer.pad_token_id,
                    )
                gen_tokens = outputs[0][inputs.input_ids.shape[1] :]
                return self._hf_tokenizer.decode(gen_tokens, skip_special_tokens=True)
            except Exception:
                return None

        return None

    def generate_batch_llm_responses(
        self, batch_messages: List[List[Dict[str, str]]]
    ) -> List[Optional[str]]:
        """Hỗ trợ Batch Inference trên GPU L4 / RTX 5090 để tăng tốc độ xử lý."""
        if not batch_messages:
            return []
        provider = self.cfg.provider.lower()
        if provider == "transformers" and self._ensure_hf_model():
            try:
                import torch

                prompts = [
                    self._hf_tokenizer.apply_chat_template(
                        msgs, tokenize=False, add_generation_prompt=True
                    )
                    for msgs in batch_messages
                ]
                inputs = self._hf_tokenizer(
                    prompts, return_tensors="pt", padding=True, truncation=True
                ).to(self._hf_model.device)
                with torch.no_grad():
                    outputs = self._hf_model.generate(
                        **inputs,
                        max_new_tokens=self.cfg.max_new_tokens,
                        do_sample=False,
                        temperature=None,
                        top_p=None,
                        pad_token_id=self._hf_tokenizer.pad_token_id,
                    )
                in_len = inputs.input_ids.shape[1]
                results: List[Optional[str]] = []
                for i in range(len(batch_messages)):
                    gen_ids = outputs[i][in_len:]
                    results.append(
                        self._hf_tokenizer.decode(gen_ids, skip_special_tokens=True)
                    )
                return results
            except Exception:
                pass
        return [self.generate_llm_response(m) for m in batch_messages]

    def build_user_prompt(
        self,
        nq: NormalizedQuestion,
        bundle: ShopBundle,
        tctx: TimeContext,
        ents: EntityResult,
        spec: AnswerSpec,
        as_of_date: str,
    ) -> str:
        t_dict = tctx.to_legacy_dict()
        t_summary = {
            "as_of": as_of_date,
            "mode": t_dict["mode"],
            "start": t_dict["start"],
            "end": t_dict["end"],
            "cmp_start": t_dict["cmp_start"],
            "cmp_end": t_dict["cmp_end"],
            "shift": t_dict["shift"],
            "periods": [
                {"start": p["start"], "end": p["end"], "label": p["label"]}
                for p in t_dict["periods"]
            ],
        }
        e_summary = {
            k: [
                {"code": c.code, "name": c.name, "score": c.score}
                for c in v
            ]
            for k, v in ents.candidates.items()
        }
        return (
            f"### SCHEMA CỬA HÀNG\n{bundle.schema_card}\n\n"
            f"### THỜI GIAN ĐÃ TÍNH SẴN `T` (hôm nay = {as_of_date})\n"
            f"{json.dumps(t_summary, ensure_ascii=False)}\n\n"
            f"### TOP 3 ỨNG VIÊN THỰC THỂ `E` (RapidFuzz >= 0.70)\n"
            f"{json.dumps(e_summary, ensure_ascii=False) if e_summary else '(Không có thực thể cụ thể)'}\n\n"
            f"### CÂU HỎI\n"
            f"- Câu gốc: {nq.raw}\n"
            f"- Chuẩn hóa: {nq.text}\n"
            f"- Kiểu đầu ra bắt buộc (`type`): `{spec.ans_type}`"
            + (f" (top k = {spec.k})" if spec.k else "")
            + "\nHãy viết đoạn code Python Pandas tính toán và gán vào `final_result`."
        )

    def synthesize_deterministic_code(
        self,
        nq: NormalizedQuestion,
        bundle: ShopBundle,
        tctx: TimeContext,
        ents: EntityResult,
        spec: AnswerSpec,
    ) -> Optional[str]:
        """
        Bộ sinh mã Pandas tất định (Deterministic Code Synthesizer) kế thừa toàn bộ
        các mẫu truy vấn chuẩn xác từ `question_parser.py` & `query_executors.py` của code cũ,
        và mở rộng cho các câu hỏi tỷ trọng nhóm hàng, hiệu ứng tăng giá, tần suất khách mua.
        Code được sinh ra sẽ chạy trực tiếp qua chính Sandbox để đảm bảo tính thống nhất 100%.
        """
        q_a = nq.ascii
        q_t = nq.text
        caps = bundle.profile.capabilities

        # Chặn các câu hỏi về chỉ số chuyên biệt chưa có trong bộ mẫu tất định để nhường cho Qwen LLM
        unsupported_for_fastpath = [
            r"\blai rong\b",
            r"\blai gop\b",
            r"\bloi nhuan\b",
            r"\bloi nhat\b",
            r"\bgia von\b",
            r"\bton kho\b",
            r"\bcon ton\b",
            r"\bhet hang\b",
            r"\bhan su dung\b",
            r"\bhet han\b",
            r"\bso lo\b",
            r"\bma lo\b",
            r"\bnhan vien\b",
            r"\btho\b",
            r"\bhoa hong\b",
            r"\bcham chi\b",
            r"\bdung ca\b",
            r"\bkhach trung thanh\b",
            r"\bkhach sop\b",
            r"\bkhach quen\b",
            r"\bhay doi y\b",
            r"\bnhom khach\b",
            r"\bkhach thanh vien\b",
            r"\bthanh vien moi\b",
            r"\bdang ky\b",
            r"\bdung diem\b",
            r"\btich diem\b",
            r"\bvoucher\b",
            r"\bphi ship\b",
            r"\bphi giao\b",
            r"\bgio cao diem\b",
            r"\bkhung gio\b",
            r"\bdat khach nhat\b",
            r"\bban so\b",
            r"\bban nao\b",
            r"\bkhu vuc\b",
            r"\bphong lanh\b",
            r"\bphi phuc vu\b",
            r"\bdot giao\b",
            r"\bcho giao\b",
            r"\bbien so\b",
            r"\bdong xe\b",
            r"\bchiec xe\b",
            r"\bbao hanh\b",
            r"\btra gop\b",
            r"\bqua han\b",
            r"\bthu may cu\b",
            r"\bdong may\b",
            r"\bsua so\b",
            r"\bua chuong nhat\b",
            r"\bnha cung cap\b",
            r"\bnhap tru ban\b",
            r"\bchenh nhap\b",
            r"\btuoi no\b",
            r"\bno them\b",
            r"\bthu no\b",
            r"\btra no\b",
            r"\bcon no tong cong\b",
            r"\bbao nhieu khach con no\b",
            r"\btien mat\b",
            r"\bchuyen khoan\b",
            r"\bvi dien tu\b",
            r"\bcod\b",
            r"\bkenh\b",
            r"\bsan tmdt\b",
            r"\bzalo\b",
            r"\btrung binh moi ngay thu\b",
        ]

        # 1. Doanh thu tăng liên tục qua các tháng (boolean)
        if spec.intent_hint == "continuous_increase" and tctx.periods:
            return (
                "revs = [revenue(p['start'], p['end']) for p in T['periods']]\n"
                "final_result = None if (len(revs) < 2 or any(r == 0 for r in revs)) else all(revs[i+1] > revs[i] for i in range(len(revs)-1))"
            )

        # 2. Danh sách doanh thu N tháng liên tiếp (list)
        if spec.intent_hint == "monthly_revenue_list" and tctx.periods:
            return (
                "revs = [revenue(p['start'], p['end']) for p in T['periods']]\n"
                "final_result = revs if (revs and all(r > 0 for r in revs)) else None"
            )

        # 3. Tháng có doanh thu cao nhất trong danh sách tháng (date YYYY-MM)
        if (
            spec.intent_hint == "highest_revenue_month"
            and tctx.periods
            and any(
                k in q_a
                for k in ["doanh thu", "nhieu tien nhat", "ban duoc nhieu tien nhat"]
            )
        ):
            return (
                "pairs = [(revenue(p['start'], p['end']), p['label']) for p in T['periods']]\n"
                "pairs = [x for x in pairs if x[0] > 0]\n"
                "final_result = max(pairs, key=lambda x: x[0])[1] if pairs else None"
            )

        # 4. Đếm số ngày đạt doanh thu từ X đồng trở lên
        m_days_rev = re.search(
            r"(?:bao nhieu|may|dem\s*so)\s*ngay\s*(?:dat\s*|co\s*)?doanh\s*thu\s*(?:tu|tren)\s*([\d\.,]+\s*(?:dong|d|k|trieu|tr)?)\s*(?:tro\s*len)?",
            q_a,
        )
        if m_days_rev:
            thresh = clean_currency_str(m_days_rev.group(1))
            if thresh > 0:
                return (
                    f"df = revenue_lines()\n"
                    f"daily = df.groupby('date')['amount'].sum() if not df.empty else pd.Series(dtype=float)\n"
                    f"cnt = int((daily >= {thresh}).sum())\n"
                    f"final_result = cnt if cnt > 0 else None"
                )

        # 5. Hiệu ứng tăng giá: N ngày kể từ ngày D so với N ngày ngay trước D (Rulebook §5 Mục 78)
        if tctx.rule_id == "R2_price_effect" and ents.candidates.get("item"):
            top_item = ents.candidates["item"][0].name
            is_pct = spec.ans_type == "percent"
            if is_pct:
                return (
                    f"df_after = match_item(item_lines(T['start'], T['end']), {repr(top_item)})\n"
                    f"df_before = match_item(item_lines(T['cmp_start'], T['cmp_end']), {repr(top_item)})\n"
                    f"q_after = float(df_after['qty_base'].sum()) if not df_after.empty else 0.0\n"
                    f"q_before = float(df_before['qty_base'].sum()) if not df_before.empty else 0.0\n"
                    f"final_result = growth(q_after, q_before) if (q_before > 0 and q_after > 0) else None"
                )
            else:
                return (
                    f"df_after = match_item(item_lines(T['start'], T['end']), {repr(top_item)})\n"
                    f"df_before = match_item(item_lines(T['cmp_start'], T['cmp_end']), {repr(top_item)})\n"
                    f"q_after = float(df_after['qty_base'].sum()) if not df_after.empty else 0.0\n"
                    f"q_before = float(df_before['qty_base'].sum()) if not df_before.empty else 0.0\n"
                    f"final_result = diff(q_after, q_before) if (q_before > 0 or q_after > 0) else None"
                )

        # 6. Thuốc kê đơn / không kê đơn (tỉ lệ % hoặc doanh thu) (Rulebook §6 Nhà thuốc)
        if any(k in q_a for k in ["ke don", "thuoc ke"]):
            if not caps.get("has_rx", False):
                return None
            want_rx = not any(
                k in q_a for k in ["khong ke don", "hong ke don", "ko ke don"]
            )
            if spec.ans_type == "percent" and not tctx.compare:
                return (
                    f"df = revenue_lines()\n"
                    f"p = V.get('products')\n"
                    f"rx_codes = set(p[p['is_prescription'] == {want_rx}]['item_code']) if (p is not None and 'is_prescription' in p.columns) else set()\n"
                    f"tot = float(df['amount'].sum()) if not df.empty else 0.0\n"
                    f"rx_rev = float(df[df['item_code'].isin(rx_codes)]['amount'].sum()) if not df.empty else 0.0\n"
                    f"final_result = pct(rx_rev, tot) if tot > 0 else None"
                )
            elif (
                spec.ans_type == "number"
                and not tctx.compare
                and any(k in q_a for k in ["tien", "doanh thu", "thu ve", "ban duoc"])
            ):
                return (
                    f"df = revenue_lines()\n"
                    f"p = V.get('products')\n"
                    f"rx_codes = set(p[p['is_prescription'] == {want_rx}]['item_code']) if (p is not None and 'is_prescription' in p.columns) else set()\n"
                    f"rx_rev = float(df[df['item_code'].isin(rx_codes)]['amount'].sum()) if not df.empty else 0.0\n"
                    f"final_result = money(rx_rev) if rx_rev > 0 else None"
                )

        # 7. Doanh thu theo hoạt chất (Rulebook §6 Nhà thuốc)
        m_act = re.search(
            r"hoat chat\s+([^?]+?)(?:\s+thang|\s+tuan|\s+ban|\s+trong|\?|$)", q_a
        )
        if (
            (m_act or ents.candidates.get("active_ingredient"))
            and "hoat chat" in q_a
            and caps.get("has_active", False)
            and spec.ans_type == "number"
            and any(k in q_a for k in ["doanh thu", "tien", "thu ve"])
        ):
            act_name = (
                ents.candidates["active_ingredient"][0].name
                if ents.candidates.get("active_ingredient")
                else m_act.group(1).strip()
            )
            return (
                f"df = revenue_lines()\n"
                f"p = V.get('products')\n"
                f"sub_p = p[p['active_ingredient'].str.lower().str.strip() == {repr(act_name.lower())}] if (p is not None and 'active_ingredient' in p.columns) else pd.DataFrame()\n"
                f"codes = set(sub_p['item_code']) if not sub_p.empty else set()\n"
                f"rev = float(df[df['item_code'].isin(codes)]['amount'].sum()) if (not df.empty and codes) else 0.0\n"
                f"final_result = money(rev) if rev > 0 else None"
            )

        # 8. Bảng Top K khách nợ nhiều nhất (table)
        if spec.intent_hint == "top_debt_table" and caps.get("has_debt", False):
            k_val = spec.k or 3
            return (
                f"odf = V.get('orders')\n"
                f"if odf is None or odf.empty:\n"
                f"    final_result = None\n"
                f"else:\n"
                f"    sub = odf[(odf['status_norm'] != 'huy') & (~odf['payment_method'].str.lower().str.contains('cod|sàn|shopee|lazada|tiki|tiktok', na=False))]\n"
                f"    sub = sub[(sub['date'] <= as_of_date) & (sub['debt'] > 0) & (~sub['cust_name'].str.lower().str.contains('khách lẻ|khach le|^$', na=False))]\n"
                f"    grp = sub.groupby(['cust_id', 'cust_name'], as_index=False)['debt'].sum().sort_values(by=['debt', 'cust_id'], ascending=[False, True], kind='mergesort').head({k_val})\n"
                f"    rows = [[r['cust_name'], money(r['debt'])] for _, r in grp.iterrows()]\n"
                f"    final_result = {{'columns': ['khach_hang', 'so_tien_no'], 'rows': rows}} if rows else None"
            )

        # Kiểm tra các mẫu nằm trong danh sách cần nhường cho LLM nếu không khớp mẫu chuyên biệt ở trên
        if any(re.search(pat, q_a) for pat in unsupported_for_fastpath):
            return None

        # 9. Giá trị trung bình một đơn (AOV) (Rulebook §3 Mục 50)
        if any(
            k in q_a
            for k in [
                "trung binh moi don",
                "trung binh 1 don",
                "trung binh mot don",
                "trung binh tren mot don",
                "trung binh tren 1 don",
                "binh quan 1 don",
                "binh quan mot don",
                "binh quan moi don",
                "binh quan 1 phieu",
                "trung binh 1 hoa don",
                "trung binh mot phieu",
            ]
        ) and not any(k in q_a for k in ["khach nao", "nhan vien nao", "tho nao"]):
            return (
                "df = revenue_lines()\n"
                "if not df.empty and 'status' in df.columns:\n"
                "    df = df[~(df['status'].str.lower().str.contains('bảo hành|bao hanh', na=False) & (df['amount'] == 0))]\n"
                "n_ord = len(df['order_id'].unique()) if not df.empty else 0\n"
                "tot = float(df['amount'].sum()) if not df.empty else 0.0\n"
                "final_result = int((tot / n_ord) + 0.5) if (n_ord > 0 and tot > 0) else None"
            )

        # 10. Tỉ lệ đơn huỷ hoặc Số đơn huỷ (Rulebook §1 Mục 19)
        if re.search(r"\b(huy|huy don|don huy|bi huy|da huy)\b", q_a) and not any(
            k in q_a for k in ["tho", "khach", "nhan vien", "phieu nhap"]
        ):
            if spec.ans_type == "percent":
                return (
                    "sub = all_orders()\n"
                    "tot = len(sub['order_id'].unique()) if not sub.empty else 0\n"
                    "canc = len(sub[sub['status_norm'] == 'huy']['order_id'].unique()) if not sub.empty else 0\n"
                    "final_result = pct(canc, tot) if tot > 0 else None"
                )
            elif re.search(
                r"(?:huy).*(?:may|bao nhieu)\s*(?:don)|(?:may|bao nhieu|dem|so)\s*(?:don|don hang).*(?:huy)",
                q_a,
            ):
                return (
                    "sub = all_orders()\n"
                    "canc = len(sub[sub['status_norm'] == 'huy']['order_id'].unique()) if not sub.empty else 0\n"
                    "final_result = int(canc)"
                )

        # 11. Mặt hàng ế / không bán được trong kỳ (Rulebook §5 Mục 77)
        if spec.intent_hint in ("unsold_items", "unsold_items_count"):
            if not caps.get("has_items", False):
                return None
            base_code = (
                "before = filter_items_in_period(V.get('sales'), '2000-01-01', T['start'], as_of_date)\n"
                "before = before[(before['date'] < T['start']) & (~before['is_service_fee'])] if not before.empty else pd.DataFrame()\n"
                "curr = item_lines()\n"
                "unsold_codes = (set(before['item_code']) if not before.empty else set()) - (set(curr['item_code']) if not curr.empty else set())\n"
                "p = V.get('products')\n"
                "if p is not None and not p.empty and not p[p['item_code'].isin(unsold_codes)].empty:\n"
                "    names = p[p['item_code'].isin(unsold_codes)].sort_values('item_code')['item_name'].tolist()\n"
                "elif not before.empty:\n"
                "    names = before[before['item_code'].isin(unsold_codes)].drop_duplicates('item_code').sort_values('item_code')['item_name'].tolist()\n"
                "else:\n"
                "    names = []\n"
                "names = [str(n).strip() for n in names if str(n).strip().lower() not in ('khách lẻ', 'nan', 'none', '', 'đơn hàng')]\n"
            )
            if spec.intent_hint == "unsold_items_count":
                return base_code + "final_result = len(names)"
            return base_code + "final_result = names if names else None"

        # 12. Mặt hàng bán chạy nhất (Top 1 hoặc Top K, có thể lọc theo nhóm hàng) (Rulebook §3 Mục 45)
        if any(
            k in q_a
            for k in [
                "ban chay nhat",
                "ban nhieu nhat",
                "ban duoc nhieu nhat",
                "chay nhat",
                "hot nhat",
                "nhieu ly nhat",
                "nhieu chai nhat",
                "nhieu goi nhat",
                "nhieu cai nhat",
                "ban chay",
            ]
        ) and not any(
            k in q_a for k in ["ua chuong", "loi nhat", "doanh thu cao nhat", "nhieu tien nhat"]
        ):
            if not caps.get("has_items", False):
                return None
            k_val = spec.k or 1
            grp_filter = ""
            if "nhom" in q_a:
                if ents.candidates.get("group"):
                    grp_name = ents.candidates["group"][0].name
                    grp_filter = f"df = df[df['item_group'].str.lower() == {repr(grp_name.lower())}]\n"
                else:
                    m_grp = re.search(r"nhom\s+([^\s,\?]+)", q_t)
                    if m_grp:
                        g_str = m_grp.group(1).strip().lower()
                        grp_filter = f"df = df[df['item_group'].str.lower().str.contains({repr(g_str)}, na=False)]\n"
            return (
                f"df = item_lines()\n"
                f"{grp_filter}"
                f"final_result = top_k(df, value_col='qty_base', k={k_val}, key_col='item_code', name_col='item_name')"
            )

        # 13. Ngày có doanh thu cao nhất trong kỳ (date YYYY-MM-DD)
        if spec.intent_hint == "highest_revenue_date" and any(
            k in q_a
            for k in [
                "doanh thu cao nhat",
                "doanh thu nhieu nhat",
                "thu ve nhieu",
                "ban duoc nhieu tien nhat",
                "nhieu tien nhat",
            ]
        ):
            return (
                "df = revenue_lines()\n"
                "daily = df[df['date'] != ''].groupby('date', as_index=False)['amount'].sum() if not df.empty else pd.DataFrame()\n"
                "daily = daily.sort_values(by=['amount', 'date'], ascending=[False, True], kind='mergesort') if not daily.empty else daily\n"
                "final_result = str(daily.iloc[0]['date'])[:10] if not daily.empty else None"
            )

        # 14. Top K khách hàng chi tiêu nhiều tiền nhất (Rulebook §3 Mục 48)
        if any(
            k in q_a
            for k in [
                "khach chi tieu nhieu nhat",
                "khach hang chi tieu nhieu nhat",
                "khach mua nhieu tien nhat",
                "khach hang mua nhieu tien nhat",
                "ai mua hang nhieu tien nhat",
                "ai chi tieu nhieu nhat",
                "khach nao mua nhieu tien nhat",
                "khach nao chi nhieu tien nhat",
                "tong tien mua cao nhat",
            ]
        ) and not any(k in q_a for k in ["bao nhieu tien", "moi nguoi", "tung nguoi"]):
            if not caps.get("has_customer", False):
                return None
            k_val = spec.k or 1
            return (
                f"df = revenue_lines()\n"
                f"df = df[df['cust_key'] != ''] if not df.empty else df\n"
                f"final_result = top_k(df, value_col='amount', k={k_val}, key_col='cust_key', name_col='cust_name')"
            )

        # 15. Khách mua nhiều lần nhất (tính theo số đơn)
        if any(
            k in q_a
            for k in [
                "khach mua nhieu lan nhat",
                "khach nao mua nhieu don hang nhat",
                "khach nao mua nhieu don nhat",
            ]
        ):
            if not caps.get("has_customer", False):
                return None
            return (
                "df = revenue_lines()\n"
                "df = df[df['cust_key'] != ''] if not df.empty else df\n"
                "if df.empty:\n"
                "    final_result = None\n"
                "else:\n"
                "    grp = df.groupby(['cust_key', 'cust_name'], as_index=False)['order_id'].nunique()\n"
                "    grp = grp.sort_values(by=['order_id', 'cust_key'], ascending=[False, True], kind='mergesort')\n"
                "    final_result = str(grp.iloc[0]['cust_name']) if not grp.empty else None"
            )

        # 16. Đếm số khách hoặc tỉ lệ % khách mua từ N đơn trở lên trong kỳ
        m_cust_n_ord = re.search(
            r"(?:mua\s+)?(?:tu|it nhat)\s*(\d+)\s*don(?:\s+tro\s+len)?", q_a
        )
        if m_cust_n_ord and "khach" in q_a and caps.get("has_customer", False):
            n_min = int(m_cust_n_ord.group(1))
            if spec.ans_type == "percent":
                return (
                    f"df = revenue_lines()\n"
                    f"df = df[df['cust_key'] != ''] if not df.empty else df\n"
                    f"if df.empty:\n"
                    f"    final_result = None\n"
                    f"else:\n"
                    f"    cnts = df.groupby('cust_key')['order_id'].nunique()\n"
                    f"    tot_c = len(cnts)\n"
                    f"    qual_c = int((cnts >= {n_min}).sum())\n"
                    f"    final_result = pct(qual_c, tot_c) if tot_c > 0 else None"
                )
            else:
                return (
                    f"df = revenue_lines()\n"
                    f"df = df[df['cust_key'] != ''] if not df.empty else df\n"
                    f"cnts = df.groupby('cust_key')['order_id'].nunique() if not df.empty else pd.Series(dtype=int)\n"
                    f"qual_c = int((cnts >= {n_min}).sum())\n"
                    f"final_result = qual_c if qual_c > 0 else None"
                )

        # 17. Giá nhập bình quân 1 mặt hàng cụ thể trong kỳ
        if any(
            k in q_a
            for k in ["nhap vao gia binh quan", "gia nhap binh quan", "nhap binh quan"]
        ):
            if not caps.get("has_imports", False) or not ents.candidates.get("item"):
                return None
            top_item = ents.candidates["item"][0].name
            return (
                f"imp = V.get('imports')\n"
                f"if imp is None or imp.empty:\n"
                f"    final_result = None\n"
                f"else:\n"
                f"    sub = imp[(imp['date'] >= T['start']) & (imp['date'] <= T['end']) & (imp['date'] <= as_of_date)]\n"
                f"    sub = sub[~sub['status'].str.lower().str.contains('huỷ|hủy|huy|cancel', na=False)]\n"
                f"    m = match_item(sub, {repr(top_item)})\n"
                f"    tot_amt = float(m['total_amount'].sum()) if not m.empty else 0.0\n"
                f"    tot_qty = float(m['qty'].sum()) if not m.empty else 0.0\n"
                f"    final_result = money(tot_amt / tot_qty) if (tot_qty > 0 and tot_amt > 0) else None"
            )

        # 18. Tổng tiền nhập hàng của tất cả các phiếu nhập trong kỳ (Rulebook §5 Mục 72)
        if any(
            k in q_a
            for k in [
                "gia tri cac phieu nhap",
                "tien nhap hang",
                "nhap hang het bao nhieu",
                "tong tien nhap",
                "bo ra bao nhieu tien nhap hang",
            ]
        ) and not any(k in q_a for k in ["nha cung cap", "mat hang", "san pham", "nao"]):
            if not caps.get("has_imports", False):
                return None
            return (
                "imp = V.get('imports')\n"
                "if imp is None or imp.empty:\n"
                "    final_result = None\n"
                "else:\n"
                "    sub = imp[(imp['date'] >= T['start']) & (imp['date'] <= T['end']) & (imp['date'] <= as_of_date)]\n"
                "    sub = sub[~sub['status'].str.lower().str.contains('huỷ|hủy|huy|cancel', na=False)]\n"
                "    if not sub.empty and '_is_receipt_total' in sub.columns and sub['_is_receipt_total'].all():\n"
                "        tot = float(sub.drop_duplicates(subset=['import_id'])['total_amount'].sum())\n"
                "    else:\n"
                "        tot = float(sub['total_amount'].sum()) if not sub.empty else 0.0\n"
                "    final_result = money(tot) if tot > 0 else None"
            )

        # 19. Tổng số đơn hàng không huỷ trong kỳ (có thể kèm ngưỡng doanh thu tối thiểu)
        if not any(
            k in q_a
            for k in [
                "huy",
                "khach",
                "nhan vien",
                "tho",
                "bao hanh",
                "tra hang",
                "ghi no",
                "con no",
                "qua han",
                "giao",
            ]
        ) and (
            re.search(
                r"(?:bao nhieu|may|dem)\s*(?:so\s*)?(?:don hang|don|hoa don|bill|phieu)",
                q_a,
            )
            or re.search(
                r"(?:co|duoc)\s+(?:tong cong\s+)?(?:may|bao nhieu)\s+(?:don|bill|phieu)",
                q_a,
            )
            or re.search(r"\bso don\b", q_a)
        ):
            m_min = re.search(r"(?:tu|tren)\s*([\d\.,]+\s*(?:dong|d|k|trieu|tr)?)", q_a)
            min_rev = clean_currency_str(m_min.group(1)) if m_min else 0.0
            return (
                f"df = revenue_lines()\n"
                f"ord_rev = df.groupby('order_id')['amount'].sum() if not df.empty else pd.Series(dtype=float)\n"
                f"ord_rev = ord_rev[ord_rev >= {min_rev}] if {min_rev} > 0 else ord_rev\n"
                f"cnt = int(len(ord_rev))\n"
                f"final_result = cnt if cnt > 0 else None"
            )

        # 20. Tăng trưởng % hoặc Chênh lệch doanh thu giữa 2 kỳ / 2 ca (Rulebook §1 Mục 20-21)
        if any(
            k in q_a
            for k in [
                "tang truong",
                "tang bao nhieu %",
                "thay doi bao nhieu %",
                "thay doi may phan tram",
                "cao hay thap hon",
                "hon hay kem",
                "tang hay giam",
                "so voi cung ky",
                "so voi thang truoc",
                "so voi thang lien truoc",
                "so voi nam truoc",
                "so voi truoc tet",
                "ngay truoc tet",
                "tet nam truoc",
                "tet nam ngoai",
            ]
        ) and not any(
            k in q_a
            for k in [
                "chiem",
                "ti trong",
                "ty trong",
                "ke don",
                "bao hanh",
                "don bi huy",
                "don huy",
                "khach",
                "nhom",
                "mon",
                "mat hang",
                "san pham",
                "tang gia",
                "so vien",
                "so luong",
            ]
        ):
            is_pct = spec.ans_type == "percent"
            if tctx.compare_shift and not tctx.compare:
                # So sánh ca chiều với ca sáng trong cùng kỳ
                return (
                    f"r_curr = revenue(T['start'], T['end'], T['shift'])\n"
                    f"r_prev = revenue(T['start'], T['end'], T['cmp_shift'])\n"
                    + (
                        "final_result = growth(r_curr, r_prev) if (r_prev > 0 and r_curr > 0) else None"
                        if is_pct
                        else "final_result = diff(r_curr, r_prev) if (r_prev > 0 or r_curr > 0) else None"
                    )
                )
            elif tctx.compare:
                return (
                    f"r_curr = revenue(T['start'], T['end'], T['shift'])\n"
                    f"r_prev = revenue(T['cmp_start'], T['cmp_end'], T['shift'])\n"
                    + (
                        "final_result = growth(r_curr, r_prev) if (r_prev > 0 and r_curr > 0) else None"
                        if is_pct
                        else "final_result = diff(r_curr, r_prev) if (r_prev > 0 or r_curr > 0) else None"
                    )
                )

        # 21. Doanh thu hoặc Tỉ trọng % của một Nhóm hàng (Rulebook §3 Mục 44)
        if "nhom" in q_a and ents.candidates.get("group"):
            grp_name = ents.candidates["group"][0].name
            if spec.ans_type == "percent" and not tctx.compare:
                return (
                    f"df = revenue_lines()\n"
                    f"tot = float(df['amount'].sum()) if not df.empty else 0.0\n"
                    f"g_rev = float(df[df['item_group'].str.lower() == {repr(grp_name.lower())}]['amount'].sum()) if not df.empty else 0.0\n"
                    f"final_result = pct(g_rev, tot) if tot > 0 else None"
                )
            elif spec.ans_type == "number" and any(
                k in q_a for k in ["doanh thu", "ban duoc bao nhieu tien", "thu ve"]
            ):
                return (
                    f"df = revenue_lines()\n"
                    f"g_rev = float(df[df['item_group'].str.lower() == {repr(grp_name.lower())}]['amount'].sum()) if not df.empty else 0.0\n"
                    f"final_result = money(g_rev) if g_rev > 0 else None"
                )

        # 22. Câu hỏi về một Mặt hàng cụ thể (Đơn giá bán / Doanh thu / Số lượng bán)
        if ents.candidates.get("item") and caps.get("has_items", False):
            top_item = ents.candidates["item"][0].name
            # 22a. Đơn giá bán 1 đơn vị của mặt hàng vào ngày D
            if (
                any(k in q_a for k in ["don gia", "gia ban", "gia bao nhieu"])
                and any(k in q_a for k in ["mot ", " 1 "])
                and not any(k in q_a for k in ["trung binh", "binh quan"])
            ):
                return (
                    f"df = match_item(revenue_lines(), {repr(top_item)})\n"
                    f"pos = df[df['unit_price'] > 0] if not df.empty else pd.DataFrame()\n"
                    f"final_result = money(pos['unit_price'].min()) if not pos.empty else None"
                )
            # 22b. Doanh thu / tiền bán của mặt hàng cụ thể
            if any(
                k in q_a
                for k in ["doanh thu", "tien ban", "bao nhieu tien", "thu ve"]
            ) and spec.ans_type == "number":
                return (
                    f"df = match_item(revenue_lines(), {repr(top_item)})\n"
                    f"rev = float(df['amount'].sum()) if not df.empty else 0.0\n"
                    f"final_result = money(rev) if rev > 0 else None"
                )
            # 22c. Số lượng bán của mặt hàng cụ thể
            if (
                any(k in q_a for k in ["ban duoc", "ban ra", "so kg", "so luong"])
                and "bao nhieu" in q_a
                and "tien" not in q_a
                and spec.ans_type == "number"
            ):
                return (
                    f"df = match_item(item_lines(), {repr(top_item)})\n"
                    f"q = float(df['qty_base'].sum()) if not df.empty else 0.0\n"
                    f"final_result = (int(round(q)) if abs(q - round(q)) < 1e-6 else round(q, 2)) if q > 0 else None"
                )

        # 23. Tổng doanh thu toàn cửa hàng (hoặc theo ca sáng/chiều) trong kỳ
        if (
            any(
                k in q_a
                for k in [
                    "doanh thu",
                    "tien ban ca",
                    "ban duoc tong cong bao nhieu tien",
                    "ban duoc bao nhieu tien",
                    "thu ve bao nhieu tien",
                    "tong tien ban",
                    "ban duoc may tien",
                ]
            )
            and not ents.candidates.get("item")
            and not any(
                k in q_a
                for k in [
                    "%",
                    "phan tram",
                    "so voi",
                    "hon hay kem",
                    "tho",
                    "khach",
                    "nhan vien",
                    "moi don",
                    "nha cung cap",
                    "ke don",
                    "hoat chat",
                    "thuoc",
                    "co chua",
                    "huy",
                    "mat hang",
                    "san pham",
                    "mon",
                    "nhom",
                    "kenh",
                    "tai quay",
                    "mang ve",
                    "giao tan noi",
                    "online",
                    "shopee",
                    "grab",
                    "tiktok",
                    "dien thoai",
                    "zalo",
                    "tien mat",
                    "chuyen khoan",
                    "moi thang",
                    "tung thang",
                    "lien nhau",
                    "moi ngay",
                    "tung ngay",
                    "thang nao",
                    "ngay nao",
                    "quy nao",
                    "dem so ngay",
                    "bao nhieu ngay",
                    "may ngay",
                ]
            )
            and not re.search(
                r"(?:doanh thu|tien ban)\s+cua\s+(?!cua hang|quan|tiem|thang|tuan|ngay|hom|nam|quy|ca\b)",
                q_a,
            )
        ):
            return (
                "rev = revenue()\n"
                "final_result = rev if rev > 0 else None"
            )

        return None

    def is_valid_execution_result(
        self, res: SandboxResult, spec: AnswerSpec, nq: NormalizedQuestion
    ) -> bool:
        """
        Kiểm tra tính hợp lệ của kết quả chạy Sandbox (Gate G5):
        - Phải chạy thành công (`res.ok == True`).
        - Giá trị khác None, không phải NaN, không phải chuỗi/list/dict rỗng.
        - Bảo vệ điểm PA (Chốt chặn số 0): Nếu kết quả bằng 0 hoặc 0.0 mà câu hỏi KHÔNG hỏi về
          huỷ/trả hàng/bảo hành/khách nợ/hàng ế/tồn kho -> coi như không hợp lệ để kích hoạt Retry/Abstain.
        """
        if not res.ok:
            return False
        if res.unanswerable_reason is not None:
            return True

        val = res.value
        if val is None:
            return False
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            return False
        if isinstance(val, str) and not val.strip():
            return False
        if isinstance(val, (list, dict)) and len(val) == 0:
            return False

        q_a = nq.ascii
        zero_allowed = any(
            k in q_a
            for k in [
                "huy",
                "bao hanh",
                "tra hang",
                "khach no",
                "con no",
                "khong ban duoc",
                "bi e",
                "hang e",
                "ton kho",
                "het hang",
            ]
        )
        if spec.ans_type in ("number", "percent") and isinstance(val, (int, float)):
            if float(val) == 0.0 and not zero_allowed:
                return False
            if spec.ans_type == "percent" and abs(float(val)) > 1000.0:
                return False

        return True

    def solve(
        self,
        qid: str,
        nq: NormalizedQuestion,
        bundle: ShopBundle,
        tctx: TimeContext,
        ents: EntityResult,
        as_of_date: str,
    ) -> Dict[str, Any]:
        """
        Thực thi quy trình giải quyết 1 câu hỏi của Agent:
        1. Kiểm tra AnswerabilityGate (U1-U3): Nếu chắc chắn Unanswerable -> trả ngay `unanswerable` (+1 điểm).
        2. Kiểm tra cổng an toàn Thời gian & Thực thể (G2, G3).
        3. Thử chạy mã Pandas tất định (Fast-Path) trong Sandbox.
        4. Nếu câu hỏi vượt ngoài tập mẫu tất định và có bật LLM (Qwen2.5-Coder-14B-Instruct):
           - Vòng 1: Sinh code Pandas từ Schema + Context + Top 3 Entity -> chạy trong Sandbox.
           - Vòng 2 (Self-Correction tối đa 1 lần): Nếu Vòng 1 gặp lỗi Traceback -> gửi phản hồi
             "Mã gặp lỗi [Traceback]. Hãy quan sát lại Schema và sửa lại đoạn code!" -> chạy lại.
        5. Nếu vẫn lỗi hoặc không hợp lệ -> trả về `abstain` (0 điểm, tuyệt đối không đoán mò).
        """
        spec = AnswerSpecInferer.infer(nq, tctx)
        trace: Dict[str, Any] = {
            "normalized_text": nq.text,
            "normalized_ascii": nq.ascii,
            "time_rule": tctx.rule_id,
            "time_start": tctx.primary.start if tctx.primary else None,
            "time_end": tctx.primary.end if tctx.primary else None,
            "entity_status": ents.status,
            "inferred_type": spec.ans_type,
        }

        # Bước 3: Kiểm tra Unanswerable Gate (U1, U2, U3)
        verdict, reason, layer_id = AnswerabilityGate.check(
            nq, tctx, bundle.profile
        )
        if verdict == "unanswerable":
            trace["gate"] = layer_id
            return {
                "ans_type": "unanswerable",
                "answer": None,
                "reason": reason,
                "is_currency": spec.is_currency,
                "month_only": spec.month_only,
                "trace": trace,
            }

        # Chốt chặn G2: Câu hỏi không xác định được kỳ thời gian
        if tctx.mode == "none":
            trace["gate"] = "G2_missing_time"
            return {
                "ans_type": "abstain",
                "answer": None,
                "reason": "",
                "is_currency": spec.is_currency,
                "month_only": spec.month_only,
                "trace": trace,
            }

        # Chốt chặn G3: Câu hỏi hỏi thực thể cụ thể nhưng Max Similarity Score < 0.70
        if ents.status == "not_found" and self.cfg.provider == "heuristic_only":
            trace["gate"] = "G3_entity_below_0.70"
            return {
                "ans_type": "abstain",
                "answer": None,
                "reason": "",
                "is_currency": spec.is_currency,
                "month_only": spec.month_only,
                "trace": trace,
            }

        # Bước 4A: Thử Fast-Path Deterministic Code trong Sandbox
        det_code = self.synthesize_deterministic_code(nq, bundle, tctx, ents, spec)
        if det_code:
            det_res = self.sandbox.execute(
                det_code, bundle, tctx, ents, self.cal, as_of_date
            )
            trace["fastpath_code"] = det_code
            trace["fastpath_ok"] = det_res.ok
            if self.is_valid_execution_result(det_res, spec, nq):
                trace["gate"] = "OK_fastpath"
                return {
                    "ans_type": spec.ans_type,
                    "answer": det_res.value,
                    "reason": "",
                    "is_currency": spec.is_currency,
                    "month_only": spec.month_only,
                    "trace": trace,
                }

        # Bước 4B: Sinh mã bằng Qwen2.5-Coder-14B-Instruct + Self-Correction (Max 1 Retry)
        if self.cfg.provider.lower() != "heuristic_only":
            user_prompt = self.build_user_prompt(
                nq, bundle, tctx, ents, spec, as_of_date
            )
            messages = [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]

            # Vòng 1
            llm_out1 = self.generate_llm_response(messages)
            code1 = extract_code(llm_out1 or "")
            res1 = self.sandbox.execute(
                code1, bundle, tctx, ents, self.cal, as_of_date
            )
            trace["round1"] = {
                "code": code1,
                "ok": res1.ok,
                "error_type": res1.error_type,
                "traceback": res1.traceback,
            }

            if res1.unanswerable_reason is not None:
                trace["gate"] = "U4_llm_unanswerable"
                return {
                    "ans_type": "unanswerable",
                    "answer": None,
                    "reason": res1.unanswerable_reason,
                    "is_currency": spec.is_currency,
                    "month_only": spec.month_only,
                    "trace": trace,
                }

            if self.is_valid_execution_result(res1, spec, nq):
                trace["gate"] = "OK_llm_round1"
                return {
                    "ans_type": spec.ans_type,
                    "answer": res1.value,
                    "reason": "",
                    "is_currency": spec.is_currency,
                    "month_only": spec.month_only,
                    "trace": trace,
                }

            # Vòng 2 (Self-Correction duy nhất 1 lần)
            tb_feedback = (
                res1.traceback
                if not res1.ok
                else f"Kết quả trả về không hợp lệ hoặc bằng rỗng/None ({repr(res1.value)})."
            )
            retry_msg = (
                f"Mã gặp lỗi [{tb_feedback}]. Hãy quan sát lại Schema và sửa lại đoạn code!"
            )
            messages_retry = messages + [
                {"role": "assistant", "content": llm_out1 or ""},
                {"role": "user", "content": retry_msg},
            ]
            llm_out2 = self.generate_llm_response(messages_retry)
            code2 = extract_code(llm_out2 or "")
            res2 = self.sandbox.execute(
                code2, bundle, tctx, ents, self.cal, as_of_date
            )
            trace["round2"] = {
                "feedback": retry_msg,
                "code": code2,
                "ok": res2.ok,
                "error_type": res2.error_type,
                "traceback": res2.traceback,
            }

            if res2.unanswerable_reason is not None:
                trace["gate"] = "U4_llm_retry_unanswerable"
                return {
                    "ans_type": "unanswerable",
                    "answer": None,
                    "reason": res2.unanswerable_reason,
                    "is_currency": spec.is_currency,
                    "month_only": spec.month_only,
                    "trace": trace,
                }

            if self.is_valid_execution_result(res2, spec, nq):
                trace["gate"] = "OK_llm_round2"
                return {
                    "ans_type": spec.ans_type,
                    "answer": res2.value,
                    "reason": "",
                    "is_currency": spec.is_currency,
                    "month_only": spec.month_only,
                    "trace": trace,
                }

        # Chốt chặn an toàn cuối cùng (G4): Trả về Abstain (0 điểm) để triệt tiêu nguy cơ bị -1 điểm
        trace["gate"] = "G4_safe_abstain"
        return {
            "ans_type": "abstain",
            "answer": None,
            "reason": "",
            "is_currency": spec.is_currency,
            "month_only": spec.month_only,
            "trace": trace,
        }

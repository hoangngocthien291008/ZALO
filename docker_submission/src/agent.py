import json
import math
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .entity_matcher import EntityResult
from .formatters import (
    format_boolean,
    format_date,
    format_list,
    format_number,
    format_percent,
    format_string,
    format_table,
    to_native,
)
from .preprocessor import NormalizedQuestion, strip_accents
from .rulebook_knowledge import DynamicFewShotSelector, RulebookRetriever
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
    require_dual_execution_agreement: bool = True
    enable_p_true_gate: bool = True
    p_true_threshold: float = 0.65
    rulebook_path: Optional[str] = None


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
              hoặc đánh giá chủ quan nhân viên (tận tâm, lễ phép, chu đáo, lành nghề, tay nghề, quý mến, hay khen, thân thiện, nhiệt tình).
    - Lớp U2: Câu hỏi yêu cầu thuộc tính / bảng mà cửa hàng hoàn toàn không lưu
              (hỏi nhập hàng nhưng không có bảng nhập, hỏi tồn kho nhưng không có bảng tồn/nhập,
              hỏi lãi gộp nhưng không có giá vốn trên dòng bán, hỏi giới tính / kênh bán / lý do trả hàng khi shop không ghi).
    - Lớp U3: Kỳ thời gian được hỏi (hoặc kỳ so sánh) nằm HOÀN TOÀN ngoài khoảng ngày có dữ liệu [min_date, max_date] của shop (Rulebook §7).
    - Lớp U4: Xác nhận chứng cứ cấu trúc khi LLM trả về sentinel UNANSWERABLE(...).
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
        r"\bthan thien\b",
        r"\bnhiet tinh\b",
        r"\bde thuong\b",
        r"\bvui ve nhat\b",
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

    @classmethod
    def corroborate_llm_unanswerable(
        cls,
        nq: NormalizedQuestion,
        tctx: TimeContext,
        bundle: ShopBundle,
        ents: EntityResult,
        reason: str,
    ) -> bool:
        """
        Lớp U4 (Corroborated Unanswerable Gate):
        Chỉ cho phép trả về `unanswerable` từ sentinel của LLM khi có bằng chứng cấu trúc xác thực
        (tránh việc LLM tự ý báo unanswerable trên câu hỏi có thể trả lời gây mất -1 điểm):
        1. Câu hỏi vượt phạm vi sổ sách hoặc thiếu bảng/cột trong `capabilities`.
        2. Câu hỏi tìm một thực thể cụ thể nhưng không tồn tại trong cửa hàng (`ents.status == 'not_found'`).
        3. Kỳ thời gian hoàn toàn không có giao dịch hoặc nằm ngoài `[min_date, max_date]`.
        """
        v, _, _ = cls.check(nq, tctx, bundle.profile)
        if v == "unanswerable":
            return True
        if ents.needs_entity and ents.status == "not_found":
            return True
        q_a = nq.ascii
        caps = bundle.profile.capabilities
        if any(k in q_a for k in ["chi phi", "lai rong", "tien dien", "tien thue", "mat bang", "luong"]) and not (
            bundle.views.get("expenses") is not None and not bundle.views["expenses"].empty
        ):
            return True
        if any(k in q_a for k in ["nhap", "nha cung cap"]) and not caps.get("has_imports", False):
            return True
        if any(k in q_a for k in ["ton kho", "trong kho", "kiem ke"]) and not caps.get("has_inventory", False):
            return True
        if any(k in q_a for k in ["lai gop", "loi nhat", "gia von"]) and not caps.get("has_line_cost", False):
            return True
        if any(k in q_a for k in ["nhan vien", "tho"]) and not (
            "staff" in bundle.views.get("sales", []).columns
            and (bundle.views["sales"]["staff"].fillna("").str.strip() != "").any()
        ):
            return True
        return False


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

        if any(
            k in q_a
            for k in [
                "bang xep hang",
                "kem so tien",
                "kem doanh thu",
                "kem so luong",
                "moi nguoi bao nhieu tien",
            ]
        ):
            m_k = re.search(r"(?:top\s*)?(\b\d{1,2}\b)", q_a)
            k = int(m_k.group(1)) if (m_k and 1 <= int(m_k.group(1)) <= 10) else 3
            return AnswerSpec(ans_type="table", k=k, intent_hint="ranking_table")

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

        # 5. List số (doanh thu mỗi tháng / lần lượt là bao nhiêu / kể giúp doanh thu N tháng liền)
        if any(
            k in q_a
            for k in [
                "moi thang",
                "tung thang",
                "lan luot la bao nhieu",
                "lan luot bao nhieu",
                "moi ben bao nhieu",
            ]
        ) or (
            tctx.mode == "multi"
            and len(tctx.periods) >= 2
            and any(k in q_a for k in ["doanh thu", "tien ban"])
            and not any(k in q_a for k in ["tong", "trung binh", "binh quan", "thang nao", "tang lien tuc", "luon cao hon"])
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

        # 7. Top K mặt hàng, khách hàng hoặc nhân viên (k > 1 -> list, k == 1 -> string)
        m_top = re.search(
            r"(?<!/)(?<!-)(?<!\d)(?:top\s*|liet ke\s*|cho toi\s*|coi gium\s*)?(\b\d{1,2}\b)\s*(?:mon|mat hang|san pham|hang|khach|nhan vien|tho)(?!\s*nao\b)",
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
                "loi nhat",
                "lai gop cao nhat",
                "pho bien nhat",
                "ua chuong nhat",
                "trung thanh nhat",
                "cham chi nhat",
                "tot nhat",
            ]
        ):
            prefix_str = q_a[: m_top.start(1)].strip()
            if not re.search(r"\b(thang|nam|ngay|quy|dot|hom|tuan|tu|it nhat|hon|tren)$", prefix_str):
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
                "nhung mon nao",
                "nhung khach nao",
                "cac mat hang nao",
                "liet ke cac mat hang",
                "liet ke nhung mat hang",
                " la nhung gi",
            ]
        ):
            return AnswerSpec(ans_type="list", k=None, intent_hint="top_k_list")

        # 7b. Giờ cao điểm -> number (0..23, không phải tiền tệ)
        if any(k in q_a for k in ["gio cao diem", "khung gio cao diem", "may gio dong khach nhat", "luc may gio"]):
            return AnswerSpec(ans_type="number", is_currency=False, intent_hint="peak_hour")

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
                    "khach hang nao",
                    "nhan vien nao",
                    "tho nao",
                    "nha cung cap nao",
                    "nha cung cap chinh",
                    "ai la",
                    "ai mua",
                    "ai ban",
                    "la gi",
                    "mat hang chi",
                    "sp nao",
                    "dong xe nao",
                    "dong may nao",
                ]
            )
            and "bao nhieu" not in q_a
            and "may " not in q_a
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
                "loi nhuan",
                "no",
                "hoa hong",
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
                "bao nhieu khach",
                "may khach",
                "so khach",
                "bao nhieu phieu",
                "may phieu",
                "bao nhieu lan",
                "may lan",
                "tuoi no",
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
Nhiệm vụ của bạn là viết MỘT đoạn code Python sử dụng Pandas và các hàm Atomic Helpers chuẩn Rulebook để tính câu trả lời chính xác cho câu hỏi kinh doanh tiếng Việt, và gán kết quả cuối cùng vào biến `final_result`.

CÁC BIẾN & HÀM ĐÃ CÓ SẴN TRONG SANDBOX (KHÔNG cần import hay đọc file):
1. `V`: Từ điển các DataFrame đã làm sạch và chuẩn hóa:
   - `V['sales']`: Chi tiết dòng bán hàng (`order_id`, `date`, `hour`, `status`, `status_norm`, `is_valid`, `is_cancelled`, `is_returned`, `is_service_fee`, `item_code`, `item_name`, `item_group`, `unit`, `qty`, `qty_base`, `unit_price`, `amount`, `cost_price`, `line_cost`, `cust_id`, `cust_name`, `cust_key`, `payment_method`, `channel`, `staff`).
   - `V['orders']`: Danh sách đơn hàng (`order_id`, `date`, `status`, `status_norm`, `is_valid`, `cust_id`, `cust_name`, `total_order`, `debt`, `paid`, `payment_method`, `channel`, `staff`).
   - `V['products']`: Danh mục hàng hóa (`item_code`, `item_name`, `item_group`, `base_unit`, `price`, `cost_price`, `is_prescription`, `active_ingredient`).
   - `V['imports']`: Phiếu nhập hàng (`import_id`, `date`, `status`, `is_valid`, `supplier_id`, `supplier`, `item_code`, `item_name`, `unit`, `qty`, `qty_base`, `unit_price`, `amount`, `total_amount`, `lot`, `expiry_date`).
   - `V['inventory']`: Tồn kho & kiểm kê (`date`, `item_code`, `item_name`, `opening_stock`, `closing_stock`, `actual_closing_stock`, `lot`, `expiry_date`).
   - `V['expenses']`: Sổ chi phí (`expense_id`, `date`, `category`, `description`, `amount`).
   - `V['customers']`: Sổ khách hàng (`cust_id`, `cust_name`, `cust_group`, `phone`, `debt`).
2. `dfs`: Từ điển các bảng gốc (`dfs['<tên_file>']`).
3. `T`: Từ điển thời gian đã phân giải chính xác từ `calendar.csv` và `as_of_date`:
   - `T['start']`, `T['end']`: Ngày bắt đầu và kết thúc kỳ chính ('YYYY-MM-DD').
   - `T['cmp_start']`, `T['cmp_end']`: Ngày bắt đầu và kết thúc kỳ so sánh ('YYYY-MM-DD').
   - `T['shift']`: 'morning' (06:00-13:59) hoặc 'afternoon' (14:00-21:59) hoặc None.
   - `T['periods']`: Danh sách các kỳ tháng khi câu hỏi hỏi nhiều tháng.
4. `E`: Từ điển TOP 3 ứng viên thực thể khớp nhất từ dữ liệu (`E.get('item')`, `E.get('customer')`, `E.get('group')`, `E.get('active_ingredient')`, `E.get('staff')`, `E.get('supplier')`).
5. Các hàm Atomic Helpers chuẩn Rulebook (ƯU TIÊN SỬ DỤNG ĐỂ TRÁNH SAI SÓT LOGIC):
   - `revenue_lines(start=T['start'], end=T['end'], shift=T['shift'])`: Các dòng bán hợp lệ trong kỳ (đã loại đơn huỷ & đơn trả hàng, đã loại `date > as_of_date`).
   - `item_lines(start=T['start'], end=T['end'], shift=T['shift'])`: Như `revenue_lines` nhưng đã loại dòng 'Phí phục vụ' (dùng khi xếp hạng / đếm mặt hàng).
   - `all_orders(start=T['start'], end=T['end'])`: Mọi đơn đặt trong kỳ (dùng khi tính tỉ lệ đơn huỷ).
   - `revenue(start=T['start'], end=T['end'], shift=T['shift'])`: Tổng doanh thu thuần trong kỳ (đã làm tròn VND).
   - `gross_profit(start=T['start'], end=T['end'], shift=T['shift'])`: Tổng lãi gộp trong kỳ theo chuẩn Rulebook.
   - `top_gross_profit_item(start=T['start'], end=T['end'], k=1)`: Mặt hàng mang lại lãi gộp cao nhất.
   - `net_profit(start=T['start'], end=T['end'])`: Lãi ròng = Lãi gộp - Tổng chi phí trong kỳ.
   - `total_expense(start=T['start'], end=T['end'], category_query=None)`: Tổng chi phí trong kỳ.
   - `total_import_amount(start=T['start'], end=T['end'], supplier_query=None)`: Tổng tiền nhập hàng hợp lệ trong kỳ.
   - `top_supplier_by_amount(start=T['start'], end=T['end'], k=1)`: Nhà cung cấp nhập nhiều tiền nhất.
   - `main_supplier(start=T['start'], end=T['end'])`: Nhà cung cấp chính (nhiều phiếu nhập nhất, hòa xét tổng tiền nhập).
   - `peak_hour(start=T['start'], end=T['end'])`: Giờ cao điểm (số nguyên 0..23 có nhiều đơn hợp lệ nhất).
   - `busiest_date(start=T['start'], end=T['end'], metric='orders')`: Ngày đắt khách nhất (nhiều đơn hợp lệ nhất, trả về DD/MM/YYYY).
   - `loyal_customer(start=T['start'], end=T['end'], k=1)`: Khách trung thành nhất (nhiều đơn hợp lệ nhất, bỏ qua Khách lẻ).
   - `big_spender_customer(start=T['start'], end=T['end'], min_orders=3)`: Khách sộp nhất (>= 3 đơn hợp lệ, AOV cao nhất).
   - `fickle_customer(start=T['start'], end=T['end'])`: Khách hay đổi ý nhất (nhiều đơn huỷ nhất).
   - `popular_item(start=T['start'], end=T['end'], k=1)`: Mặt hàng phổ biến nhất (xuất hiện trong nhiều đơn hợp lệ nhất).
   - `best_staff_by_revenue(start=T['start'], end=T['end'], k=1)` & `hardworking_staff(start=T['start'], end=T['end'], k=1)`: Nhân viên doanh thu cao nhất / nhiều đơn nhất.
   - `revenue_by_payment(method_query, start=T['start'], end=T['end'])`, `revenue_by_channel(channel_query, start=T['start'], end=T['end'])`, `revenue_by_cust_group(group_query, start=T['start'], end=T['end'], metric='revenue')`.
   - `weekday_avg_revenue(iso_weekday, start=T['start'], end=T['end'])`: Doanh thu trung bình mỗi ngày Thứ X (1=Thứ Hai .. 7=Chủ Nhật).
   - `qty_by_base_unit(unit_query, start=T['start'], end=T['end'])` & `qty_by_coffee_size(size_char, start=T['start'], end=T['end'])`.
   - `money(x)`: Làm tròn tiền VND (nửa đồng làm tròn lên).
   - `pct(num, den)`: Tính phần trăm `round(num / den * 100, 2)` (KHÔNG chia 100, KHÔNG kèm '%').
   - `growth(curr, prev)`: Tính % tăng trưởng `round((curr - prev) / prev * 100, 2)`.
   - `top_k(df, value_col='qty_base', k=1, key_col='item_code', name_col='item_name')`: Xếp hạng giảm dần theo `value_col`, hòa thì ưu tiên `key_col` tăng dần.
   - `rank_table(df, key_col, name_col, value_cols, k=3, col_names=None)`: Tạo bảng kết quả chuẩn `{'columns': [...], 'rows': [...]}`.
   - `match_item(df, query)`: Lọc các dòng khớp tên mặt hàng `query`.
   - `UNANSWERABLE(reason)`: Chỉ gọi khi cơ sở dữ liệu hoàn toàn KHÔNG lưu trữ thuộc tính/bảng mà câu hỏi yêu cầu.

QUY TẮC BẮT BUỘC:
- Tuân thủ tuyệt đối mục `TRI THỨC RULEBOOK & CÔNG THỨC BẮT BUỘC` trong đề bài.
- Chỉ xuất duy nhất 1 khối ```python ... ``` và bắt buộc gán `final_result = ...` ở dòng cuối."""

    VERIFICATION_SYSTEM_PROMPT = """Bạn là Chuyên gia Kiểm toán Độc lập (Independent First-Principles Code Verifier).
Nhiệm vụ của bạn là viết MỘT đoạn code Python Pandas độc lập để kiểm chứng lại câu trả lời cho câu hỏi kinh doanh tiếng Việt và gán kết quả vào biến `final_result`.
YÊU CẦU KIỂM TOÁN BẮT BUỘC:
1. Kiểm tra kỹ từng điều kiện lọc theo đúng `TRI THỨC RULEBOOK`:
   - Lọc ngày trong khoảng `[T['start'], T['end']]` và bắt buộc `date <= as_of_date`.
   - Loại bỏ hoàn toàn đơn huỷ và đơn trả hàng (`is_valid == True`).
   - Khi xếp hạng mặt hàng: loại dòng Phí phục vụ (`~is_service_fee`), dùng `qty_base` cho số lượng bán chạy, hòa thì ưu tiên `item_code` tăng dần (`kind='mergesort'`).
   - Khi xếp hạng khách hàng: loại bỏ `'Khách lẻ'`, `'Khách vãng lai'` (`cust_key != ''`), hòa thì ưu tiên `cust_key` tăng dần.
2. Đảm bảo kiểu dữ liệu của `final_result` khớp chính xác với `Kiểu đầu ra bắt buộc (type)`.
3. Chỉ xuất duy nhất 1 khối ```python ... ``` và gán `final_result = ...` ở dòng cuối."""

    def __init__(
        self,
        llm_config: Optional[LLMConfig] = None,
        sandbox: Optional[Sandbox] = None,
        cal_resolver: Optional[CalendarResolver] = None,
    ) -> None:
        self.cfg = llm_config or LLMConfig(provider="heuristic_only")
        self.sandbox = sandbox or Sandbox(timeout_s=10.0)
        self.cal = cal_resolver
        self.rulebook_retriever = RulebookRetriever(self.cfg.rulebook_path)
        self._hf_model = None
        self._hf_tokenizer = None
        self._shop_grounding_cache: Dict[str, set] = {}

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
            "cmp_shift": t_dict.get("cmp_shift"),
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
        rulebook_evidence = self.rulebook_retriever.retrieve_evidence(
            nq, bundle, tctx, spec.ans_type
        )
        few_shot_code = DynamicFewShotSelector.select(nq, spec.ans_type)
        return (
            f"### SCHEMA CỬA HÀNG\n{bundle.schema_card}\n\n"
            f"### TRI THỨC RULEBOOK & CÔNG THỨC BẮT BUỘC (EXTERNAL KNOWLEDGE EVIDENCE)\n{rulebook_evidence}\n\n"
            f"### THỜI GIAN ĐÃ TÍNH SẴN `T` (hôm nay = {as_of_date})\n"
            f"{json.dumps(t_summary, ensure_ascii=False)}\n\n"
            f"### TOP 3 ỨNG VIÊN THỰC THỂ `E` (RapidFuzz >= 0.70)\n"
            f"{json.dumps(e_summary, ensure_ascii=False) if e_summary else '(Không có thực thể cụ thể)'}\n\n"
            f"### MẪU CODE CHUẨN TƯƠNG ỨNG (DYNAMIC FEW-SHOT EXEMPLARS)\n```python\n{few_shot_code}\n```\n\n"
            f"### CÂU HỎI\n"
            f"- Câu gốc: {nq.raw}\n"
            f"- Chuẩn hóa: {nq.text}\n"
            f"- Kiểu đầu ra bắt buộc (`type`): `{spec.ans_type}`"
            + (f" (top k = {spec.k})" if spec.k else "")
            + "\nHãy viết đoạn code Python Pandas tính toán chính xác theo đúng Rulebook và gán vào `final_result`."
        )

    def build_verification_code_prompt(
        self,
        nq: NormalizedQuestion,
        bundle: ShopBundle,
        tctx: TimeContext,
        ents: EntityResult,
        spec: AnswerSpec,
        as_of_date: str,
    ) -> str:
        """
        Xây dựng Prompt Góc nhìn Kiểm toán Độc lập (Perspective B - Decomposed First-Principles Verification)
        theo phương pháp CHESS (2024) & LEVER (ICML 2023):
        Yêu cầu mô hình viết code kiểm chứng từng bước từ các bảng chuẩn hóa `V[...]` để đối chiếu kết quả với Góc nhìn A.
        """
        t_dict = tctx.to_legacy_dict()
        t_summary = {
            "as_of": as_of_date,
            "mode": t_dict["mode"],
            "start": t_dict["start"],
            "end": t_dict["end"],
            "cmp_start": t_dict["cmp_start"],
            "cmp_end": t_dict["cmp_end"],
            "shift": t_dict["shift"],
            "cmp_shift": t_dict.get("cmp_shift"),
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
        rulebook_evidence = self.rulebook_retriever.retrieve_evidence(
            nq, bundle, tctx, spec.ans_type
        )
        return (
            f"### SCHEMA CỬA HÀNG\n{bundle.schema_card}\n\n"
            f"### QUY TẮC KIỂM TOÁN BẮT BUỘC TỪ RULEBOOK\n{rulebook_evidence}\n\n"
            f"### THỜI GIAN ĐÃ TÍNH SẴN `T` (hôm nay = {as_of_date})\n"
            f"{json.dumps(t_summary, ensure_ascii=False)}\n\n"
            f"### ỨNG VIÊN THỰC THỂ `E`\n"
            f"{json.dumps(e_summary, ensure_ascii=False) if e_summary else '(Không có thực thể cụ thể)'}\n\n"
            f"### YÊU CẦU KIỂM TOÁN TỪNG BƯỚC (DECOMPOSED STEP-BY-STEP PANDAS)\n"
            f"- Câu hỏi gốc: {nq.raw}\n"
            f"- Câu hỏi chuẩn hóa: {nq.text}\n"
            f"- Kiểu kết quả bắt buộc: `{spec.ans_type}`"
            + (f" (top k = {spec.k})" if spec.k else "")
            + "\nHãy viết đoạn code Python Pandas kiểm chứng độc lập từng bước (lọc kỳ thời gian `T['start']..T['end']`, loại `date > as_of_date`, lọc đơn hợp lệ, áp dụng đúng quy tắc làm tròn và tie-break `kind='mergesort'`) rồi gán kết quả cuối cùng vào `final_result`."
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
        kết hợp các hàm Atomic Rulebook Helpers đã kiểm chứng.
        Code được sinh ra sẽ chạy trực tiếp qua chính Sandbox để đảm bảo tính thống nhất 100%.
        """
        q_a = nq.ascii
        q_t = nq.text
        caps = bundle.profile.capabilities

        is_explicit_comparison = any(
            k in q_a
            for k in [
                "so voi",
                "tang truong",
                "tang bao nhieu",
                "giam bao nhieu",
                "thay doi bao nhieu",
                "thay doi may",
                "cao hay thap hon",
                "hon hay kem",
                "tang hay giam",
                "chenh lech",
                "nhieu hon hay it hon",
            ]
        )

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
            if spec.ans_type == "percent" and not is_explicit_comparison:
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
                and not is_explicit_comparison
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

        # 8b. Các mẫu Atomic Rulebook Helpers rõ ràng (Lãi gộp, Lãi ròng, Giờ cao điểm, Ngày đắt khách, Khách trung thành/sộp/đổi ý, Mặt hàng phổ biến, Thứ trong tuần)
        if (
            any(k in q_a for k in ["lai gop", "loi nhuan gop"])
            and not is_explicit_comparison
            and not ents.candidates.get("item")
            and not ents.candidates.get("group")
        ):
            if spec.ans_type in ("string", "list") or any(k in q_a for k in ["mat hang nao", "san pham nao", "mon nao", "cao nhat", "nhieu nhat"]):
                if spec.ans_type in ("string", "list"):
                    k_val = spec.k or 1
                    return f"final_result = top_gross_profit_item(T['start'], T['end'], k={k_val})"
            elif spec.ans_type == "number" and not any(k in q_a for k in ["mat hang", "san pham", "nhom"]):
                return "final_result = gross_profit(T['start'], T['end'], T['shift'])"

        if (
            any(k in q_a for k in ["mat hang nao loi nhat", "san pham nao loi nhat", "mon nao loi nhat", "hang nao loi nhat"])
            and spec.ans_type in ("string", "list")
        ):
            k_val = spec.k or 1
            return f"final_result = top_gross_profit_item(T['start'], T['end'], k={k_val})"

        if (
            any(k in q_a for k in ["lai rong", "loi nhuan rong"])
            and spec.ans_type == "number"
            and not is_explicit_comparison
        ):
            return "final_result = net_profit(T['start'], T['end'])"

        if spec.intent_hint == "peak_hour" and not is_explicit_comparison:
            return "final_result = peak_hour(T['start'], T['end'])"

        if (
            spec.ans_type == "date"
            and not spec.month_only
            and any(k in q_a for k in ["dat khach nhat", "dong khach nhat", "nhieu don nhat", "nhieu hoa don nhat"])
        ):
            return "final_result = busiest_date(T['start'], T['end'], metric='orders')"

        if (
            any(k in q_a for k in ["khach trung thanh nhat", "khach hang trung thanh nhat"])
            and spec.ans_type in ("string", "list")
        ):
            k_val = spec.k or 1
            return f"final_result = loyal_customer(T['start'], T['end'], k={k_val})"

        if (
            any(k in q_a for k in ["khach sop nhat", "khach hang sop nhat"])
            and spec.ans_type == "string"
        ):
            return "final_result = big_spender_customer(T['start'], T['end'])"

        if (
            any(k in q_a for k in ["khach hay doi y nhat", "khach hang hay doi y nhat", "khach nao huy don nhieu nhat", "khach nao huy nhieu don nhat"])
            and spec.ans_type == "string"
        ):
            return "final_result = fickle_customer(T['start'], T['end'])"

        if (
            any(k in q_a for k in ["pho bien nhat", "ua chuong nhat", "xuat hien trong nhieu don nhat", "gop mat trong nhieu don nhat"])
            and spec.ans_type in ("string", "list")
            and not any(k in q_a for k in ["nhom", "size", "sua so"])
        ):
            k_val = spec.k or 1
            return f"final_result = popular_item(T['start'], T['end'], k={k_val})"

        if (
            any(k in q_a for k in ["nhan vien ban tot nhat", "nhan vien nao ban tot nhat", "nhan vien co doanh thu cao nhat", "nhan vien nao doanh thu cao nhat"])
            and spec.ans_type in ("string", "list")
        ):
            k_val = spec.k or 1
            return f"final_result = best_staff_by_revenue(T['start'], T['end'], k={k_val})"

        if (
            any(k in q_a for k in ["nhan vien cham chi nhat", "nhan vien nao cham chi nhat", "nhan vien nao len nhieu don nhat"])
            and spec.ans_type in ("string", "list")
        ):
            k_val = spec.k or 1
            return f"final_result = hardworking_staff(T['start'], T['end'], k={k_val})"

        if "nha cung cap chinh" in q_a and spec.ans_type == "string":
            return "final_result = main_supplier(T['start'], T['end'])"

        if (
            any(k in q_a for k in ["nha cung cap nao", "nhap cua nha cung cap nao"])
            and any(k in q_a for k in ["nhieu tien nhat", "gia tri cao nhat", "lon nhat"])
            and spec.ans_type in ("string", "list")
        ):
            k_val = spec.k or 1
            return f"final_result = top_supplier_by_amount(T['start'], T['end'], k={k_val})"

        # Doanh thu trung bình mỗi ngày Thứ X (Rulebook §2 Mục 38)
        m_wd = re.search(
            r"(?:trung binh|binh quan)\s+(?:moi\s+)?ngay\s+(thu\s+hai|thu\s+ba|thu\s+tu|thu\s+nam|thu\s+sau|thu\s+bay|chu\s+nhat|t2|t3|t4|t5|t6|t7|cn)\b",
            q_a,
        )
        if m_wd and spec.ans_type == "number" and not is_explicit_comparison:
            wd_map = {
                "thu hai": 1, "t2": 1,
                "thu ba": 2, "t3": 2,
                "thu tu": 3, "t4": 3,
                "thu nam": 4, "t5": 4,
                "thu sau": 5, "t6": 5,
                "thu bay": 6, "t7": 6,
                "chu nhat": 7, "cn": 7,
            }
            wd_key = re.sub(r"\s+", " ", m_wd.group(1).strip())
            if wd_key in wd_map:
                return f"final_result = weekday_avg_revenue({wd_map[wd_key]}, T['start'], T['end'])"

        # Kiểm tra các mẫu nằm trong danh sách cần nhường cho LLM nếu không khớp mẫu chuyên biệt ở trên
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
            r"\btien tuoi\b",
            r"\bxi o di\b",
            r"\bvi momo\b",
            r"\bchuyen khoan\b",
            r"\bvi dien tu\b",
            r"\bcod\b",
            r"\bkenh\b",
            r"\bsan tmdt\b",
            r"\bzalo\b",
            r"\btrung binh moi ngay thu\b",
        ]
        if any(re.search(pat, q_a) for pat in unsupported_for_fastpath) or re.search(
            r"\b(?:số lô|mã lô)\b", q_t
        ):
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
                "moi bill",
                "1 bill",
                "mot bill",
                "moi hoa don",
                "1 hoa don",
                "moi bil",
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
        ) and not ents.candidates.get("item"):
            is_pct = spec.ans_type == "percent"
            if tctx.compare_shift:
                # Ưu tiên so sánh ca chiều với ca sáng trong cùng kỳ (ngay cả khi tctx.compare có giá trị mặc định của tháng)
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
            if spec.ans_type == "percent" and not is_explicit_comparison:
                return (
                    f"df = revenue_lines()\n"
                    f"tot = float(df['amount'].sum()) if not df.empty else 0.0\n"
                    f"g_rev = float(df[df['item_group'].str.lower() == {repr(grp_name.lower())}]['amount'].sum()) if not df.empty else 0.0\n"
                    f"final_result = pct(g_rev, tot) if tot > 0 else None"
                )
            elif (
                spec.ans_type == "number"
                and not is_explicit_comparison
                and any(k in q_a for k in ["doanh thu", "ban duoc bao nhieu tien", "thu ve"])
            ):
                return (
                    f"df = revenue_lines()\n"
                    f"g_rev = float(df[df['item_group'].str.lower() == {repr(grp_name.lower())}]['amount'].sum()) if not df.empty else 0.0\n"
                    f"final_result = money(g_rev) if g_rev > 0 else None"
                )

        # 22. Câu hỏi về một Mặt hàng cụ thể (Đơn giá bán / Doanh thu / Số lượng bán / So sánh giữa 2 kỳ)
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
            if (
                any(k in q_a for k in ["doanh thu", "tien ban", "bao nhieu tien", "thu ve"])
                and spec.ans_type == "number"
                and not is_explicit_comparison
            ):
                return (
                    f"df = match_item(revenue_lines(), {repr(top_item)})\n"
                    f"rev = float(df['amount'].sum()) if not df.empty else 0.0\n"
                    f"final_result = money(rev) if rev > 0 else None"
                )
            # 22c. Số lượng bán của mặt hàng cụ thể
            if (
                any(k in q_a for k in ["ban duoc", "ban ra", "so kg", "so luong", "so lo", "so chai", "so lon", "so hop", "so vien", "so goi"])
                and ("bao nhieu" in q_a or "may " in q_a)
                and "tien" not in q_a
                and spec.ans_type == "number"
                and not is_explicit_comparison
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

    def _get_shop_grounding_strings(self, bundle: ShopBundle) -> set:
        """
        Tập hợp toàn bộ các tên thực thể có thật trong cửa hàng (mặt hàng, khách hàng, nhóm hàng,
        nhân viên, nhà cung cấp, hoạt chất, phương thức thanh toán, kênh bán, mã đơn, v.v.)
        để kiểm tra Entity Grounding (chống ảo giác tên thực thể của SLM).
        """
        bid = bundle.profile.business_id
        if bid in self._shop_grounding_cache:
            return self._shop_grounding_cache[bid]
        grounded: set = set()
        for entries in bundle.entity_index.values():
            for code, name, norm_a in entries:
                if code:
                    grounded.add(strip_accents(str(code).lower()).strip())
                if name:
                    grounded.add(strip_accents(str(name).lower()).strip())
                if norm_a:
                    grounded.add(norm_a.strip())
        for vdf in bundle.views.values():
            if vdf is None or vdf.empty:
                continue
            for col in vdf.columns:
                if vdf[col].dtype == object:
                    for val in vdf[col].dropna().unique()[:500]:
                        s_val = strip_accents(str(val).lower()).strip()
                        if 1 <= len(s_val) <= 120:
                            grounded.add(s_val)
        self._shop_grounding_cache[bid] = grounded
        return grounded

    def is_valid_execution_result(
        self,
        res: SandboxResult,
        spec: AnswerSpec,
        nq: NormalizedQuestion,
        bundle: Optional[ShopBundle] = None,
    ) -> bool:
        """
        Kiểm tra tính hợp lệ ngữ nghĩa nghiêm ngặt của kết quả chạy Sandbox (Gate G5 - Strict Semantic Validator):
        1. Phải chạy thành công (`res.ok == True`), khác None, không phải NaN/Inf, không phải chuỗi/list/dict rỗng.
        2. Kiểm tra khớp kiểu dữ liệu (`ans_type`) và miền giá trị hợp lý (Domain Range Check):
           - `number`: không phải boolean; nếu hỏi giờ cao điểm thì phải thuộc `0..23`; nếu không hỏi chênh lệch/lãi/tồn kho thì không được âm.
           - `percent`: nếu hỏi tỷ trọng (`chiếm bao nhiêu %`, `tỷ trọng`) thì bắt buộc `0 < val <= 100`; nếu hỏi tăng trưởng thì `-100 <= val <= 1000`.
           - `string`: phải có thật trong dữ liệu cửa hàng (Entity Grounding Check) và không phải `'Khách lẻ'`, `'nan'`, `'none'`.
           - `list`: độ dài `>= 1` (và `<= spec.k` nếu câu hỏi yêu cầu Top K cố định).
           - `table`: có đủ `columns` và `rows` không rỗng.
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
                "chenh lech",
                "kiem ke",
            ]
        )
        negative_allowed = any(
            k in q_a
            for k in [
                "chenh lech",
                "thay doi",
                "tang truong",
                "tang hay giam",
                "hon hay kem",
                "cao hay thap",
                "lai",
                "loi nhuan",
                "kiem ke",
                "that thoat",
            ]
        )

        if spec.ans_type == "boolean":
            return isinstance(val, bool)

        if spec.ans_type in ("number", "percent"):
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                return False
            fval = float(val)
            if fval == 0.0 and not zero_allowed:
                return False
            if fval < 0.0 and not negative_allowed:
                return False
            if spec.intent_hint == "peak_hour" and not (0 <= fval <= 23 and abs(fval - round(fval)) < 1e-6):
                return False
            if spec.ans_type == "percent":
                if abs(fval) > 1000.0:
                    return False
                is_share_query = any(
                    k in q_a for k in ["chiem", "ty trong", "ti trong"]
                ) and not any(k in q_a for k in ["so voi", "tang", "giam", "thay doi"])
                if is_share_query and not (0.0 < fval <= 100.0):
                    return False
            return True

        if spec.ans_type == "date":
            s_val = str(val).strip()
            return bool(
                re.match(r"^(?:\d{4}-\d{2}(?:-\d{2})?|\d{2}/\d{2}/\d{4}|\d{1,2}/\d{4})$", s_val)
            )

        if spec.ans_type == "string":
            if not isinstance(val, str):
                return False
            s_clean = val.strip()
            s_low = s_clean.lower()
            if s_low in ("nan", "none", "null", "true", "false", "khách lẻ", "khach le", "khách vãng lai", "đơn hàng"):
                return False
            if re.match(r"^-?\d+(?:\.\d+)?$", s_clean):
                return False
            if bundle is not None:
                grounded = self._get_shop_grounding_strings(bundle)
                s_norm = strip_accents(s_low).strip()
                s_norm_compact = re.sub(r"(\d+)\s+(ml|l|g|kg|mg|mcg|gb|tb|mm|cm)\b", r"\1\2", s_norm)
                if (
                    s_norm not in grounded
                    and s_norm_compact not in grounded
                    and not any(s_norm in g or g in s_norm for g in grounded if len(g) >= 3)
                ):
                    return False
            return True

        if spec.ans_type == "list":
            if not isinstance(val, (list, tuple)) or len(val) == 0:
                return False
            if spec.k is not None and len(val) > spec.k:
                return False
            for item in val:
                if item is None or str(item).strip().lower() in ("", "nan", "none", "null"):
                    return False
            return True

        if spec.ans_type == "table":
            if not isinstance(val, dict):
                return False
            cols = val.get("columns")
            rows = val.get("rows")
            if not isinstance(cols, list) or not isinstance(rows, list) or len(cols) == 0 or len(rows) == 0:
                return False
            return True

        return True

    def results_agree(self, val_a: Any, val_b: Any, spec: AnswerSpec) -> bool:
        """
        Kiểm tra sự đồng thuận thực thi giữa 2 góc nhìn sinh mã độc lập (Gate G6 - Dual-Execution Consistency):
        - Góc nhìn A (Standard Atomic Code-Act) vs Góc nhìn B (Decomposed Step-by-Step Verification).
        - Chỉ khi cả 2 góc nhìn thực thi ra cùng một đáp án thì mới được phép xuất kết quả của SLM.
        """
        if val_a is None or val_b is None:
            return False
        try:
            if spec.ans_type == "number":
                fa, fb = float(val_a), float(val_b)
                tol = 1.0 if spec.is_currency else 1e-2
                return abs(fa - fb) <= tol
            if spec.ans_type == "percent":
                fa, fb = float(val_a), float(val_b)
                return abs(fa - fb) <= 0.05
            if spec.ans_type == "boolean":
                return format_boolean(val_a) == format_boolean(val_b)
            if spec.ans_type == "date":
                return format_date(val_a, month_only=spec.month_only) == format_date(
                    val_b, month_only=spec.month_only
                )
            if spec.ans_type == "string":
                sa = strip_accents(format_string(val_a).lower()).strip()
                sb = strip_accents(format_string(val_b).lower()).strip()
                return sa == sb
            if spec.ans_type == "list":
                la = format_list(val_a)
                lb = format_list(val_b)
                if len(la) != len(lb):
                    return False
                for xa, xb in zip(la, lb):
                    if isinstance(xa, (int, float)) and isinstance(xb, (int, float)):
                        if abs(float(xa) - float(xb)) > 1.0:
                            return False
                    else:
                        if strip_accents(str(xa).lower()).strip() != strip_accents(str(xb).lower()).strip():
                            return False
                return True
            if spec.ans_type == "table":
                ta = format_table(val_a)
                tb = format_table(val_b)
                return ta.get("rows") == tb.get("rows")
        except Exception:
            return False
        return val_a == val_b

    def _build_p_true_prompt(
        self,
        nq: NormalizedQuestion,
        bundle: ShopBundle,
        tctx: TimeContext,
        spec: AnswerSpec,
        code: str,
        exec_val: Any,
    ) -> List[Dict[str, str]]:
        """Xây dựng prompt tự đánh giá xác suất đúng P(True) theo Kadavath et al. (2022)."""
        evidence = self.rulebook_retriever.retrieve_evidence(
            nq, bundle, tctx, spec.ans_type
        )
        prompt = (
            f"### CÂU HỎI\n{nq.raw}\n\n"
            f"### QUY TẮC RULEBOOK BẮT BUỘC\n{evidence}\n\n"
            f"### ĐOẠN MÃ ĐÃ CHẠY\n```python\n{code}\n```\n\n"
            f"### KẾT QUẢ THỰC THI (`{spec.ans_type}`)\n{repr(exec_val)}\n\n"
            f"Đoạn mã và kết quả trên có tuân thủ đúng 100% quy tắc Rulebook và trả lời chính xác câu hỏi không?\n"
            f"A. Đúng hoàn toàn\n"
            f"B. Có sai sót hoặc thiếu điều kiện\n"
            f"Chỉ xuất duy nhất 1 chữ cái A hoặc B:"
        )
        return [
            {"role": "system", "content": "Bạn là giám khảo kiểm định chất lượng mã nguồn Python Pandas theo Rulebook."},
            {"role": "user", "content": prompt},
        ]

    def compute_p_true_confidence(
        self,
        nq: NormalizedQuestion,
        bundle: ShopBundle,
        tctx: TimeContext,
        spec: AnswerSpec,
        code: str,
        exec_val: Any,
    ) -> float:
        """
        Tính xác suất P(True) = P('A') / (P('A') + P('B')) trực tiếp từ Logits của mô hình Qwen
        theo bài báo 'Language Models (Mostly) Know What They Know' (Kadavath et al., 2022).
        """
        if not self.cfg.enable_p_true_gate:
            return 1.0
        if self.cfg.provider.lower() != "transformers" or not self._ensure_hf_model():
            return 1.0
        try:
            import torch

            msgs = self._build_p_true_prompt(nq, bundle, tctx, spec, code, exec_val)
            prompt_text = self._hf_tokenizer.apply_chat_template(
                msgs, tokenize=False, add_generation_prompt=True
            )
            inputs = self._hf_tokenizer([prompt_text], return_tensors="pt").to(
                self._hf_model.device
            )
            with torch.no_grad():
                out = self._hf_model(**inputs)
                next_logits = out.logits[0, -1, :]
            id_a = self._hf_tokenizer.encode("A", add_special_tokens=False)[0]
            id_b = self._hf_tokenizer.encode("B", add_special_tokens=False)[0]
            la = float(next_logits[id_a].item())
            lb = float(next_logits[id_b].item())
            max_l = max(la, lb)
            pa = math.exp(la - max_l)
            pb = math.exp(lb - max_l)
            return pa / (pa + pb)
        except Exception:
            return 1.0

    def compute_batch_p_true_confidence(
        self,
        items: List[Tuple[NormalizedQuestion, ShopBundle, TimeContext, AnswerSpec, str, Any]],
    ) -> List[float]:
        """Tính P(True) theo batch trên GPU để tối ưu tốc độ trên Google Colab."""
        if not items:
            return []
        if not self.cfg.enable_p_true_gate:
            return [1.0] * len(items)
        if self.cfg.provider.lower() != "transformers" or not self._ensure_hf_model():
            return [1.0] * len(items)
        try:
            import torch

            prompts = [
                self._hf_tokenizer.apply_chat_template(
                    self._build_p_true_prompt(nq, bundle, tctx, spec, code, val),
                    tokenize=False,
                    add_generation_prompt=True,
                )
                for (nq, bundle, tctx, spec, code, val) in items
            ]
            inputs = self._hf_tokenizer(
                prompts, return_tensors="pt", padding=True, truncation=True
            ).to(self._hf_model.device)
            with torch.no_grad():
                out = self._hf_model(**inputs)
                next_logits = out.logits[:, -1, :]
            id_a = self._hf_tokenizer.encode("A", add_special_tokens=False)[0]
            id_b = self._hf_tokenizer.encode("B", add_special_tokens=False)[0]
            probs: List[float] = []
            for i in range(len(items)):
                la = float(next_logits[i, id_a].item())
                lb = float(next_logits[i, id_b].item())
                max_l = max(la, lb)
                pa = math.exp(la - max_l)
                pb = math.exp(lb - max_l)
                probs.append(pa / (pa + pb))
            return probs
        except Exception:
            return [self.compute_p_true_confidence(*it) for it in items]

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
        Thực thi quy trình giải quyết 1 câu hỏi của Agent với 3 trụ cột tối ưu:
        1. Kiểm tra AnswerabilityGate (U1-U3): Nếu chắc chắn Unanswerable -> trả ngay `unanswerable` (+1 điểm).
        2. Kiểm tra cổng an toàn Thời gian & Thực thể (G2, G3).
        3. Thử chạy mã Pandas tất định (Fast-Path + Atomic Rulebook Helpers) trong Sandbox.
        4. Nếu câu hỏi vượt ngoài tập mẫu tất định và có bật LLM (Qwen2.5-Coder-14B-Instruct):
           - Tiêm Tri thức Rulebook động (`RulebookRetriever`) + Dynamic Few-Shot Exemplars vào Prompt.
           - Vòng 1 & Vòng 2 (Self-Correction tối đa 1 lần): Sinh code Góc nhìn A -> chạy trong Sandbox.
           - Cổng chấp nhận kết quả nghiêm ngặt (Gate G5 + Gate G6):
             + Kiểm chứng ngữ nghĩa & Entity Grounding (`is_valid_execution_result`).
             + Kiểm chứng Thực thi Kép độc lập (Dual-Perspective Execution Verification: `code_A` vs `code_B`).
             + Kiểm định độ tự tin Logprob P(True) >= tau (Kadavath Calibration Gate).
        5. Nếu không vượt qua đồng thời các cổng kiểm định -> trả về `abstain` (0 điểm, tuyệt đối không để bị -1 điểm).
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
                det_code, bundle, tctx, ents, self.cal, as_of_date, fast_local=True
            )
            trace["fastpath_code"] = det_code
            trace["fastpath_ok"] = det_res.ok
            if det_res.unanswerable_reason is not None:
                if AnswerabilityGate.corroborate_llm_unanswerable(
                    nq, tctx, bundle, ents, det_res.unanswerable_reason
                ):
                    trace["gate"] = "U2_fastpath_unanswerable"
                    return {
                        "ans_type": "unanswerable",
                        "answer": None,
                        "reason": det_res.unanswerable_reason,
                        "is_currency": spec.is_currency,
                        "month_only": spec.month_only,
                        "trace": trace,
                    }
            elif self.is_valid_execution_result(det_res, spec, nq, bundle):
                trace["gate"] = "OK_fastpath"
                return {
                    "ans_type": spec.ans_type,
                    "answer": det_res.value,
                    "reason": "",
                    "is_currency": spec.is_currency,
                    "month_only": spec.month_only,
                    "trace": trace,
                }

        # Bước 4B: Sinh mã bằng Qwen2.5-Coder-14B-Instruct + Self-Correction + Dual-Verification Gate
        if self.cfg.provider.lower() != "heuristic_only":
            user_prompt = self.build_user_prompt(
                nq, bundle, tctx, ents, spec, as_of_date
            )
            messages = [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]

            # Vòng 1 (Góc nhìn A)
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

            cand_code: Optional[str] = None
            cand_res: Optional[SandboxResult] = None
            cand_stage: str = ""

            if res1.unanswerable_reason is not None:
                if AnswerabilityGate.corroborate_llm_unanswerable(
                    nq, tctx, bundle, ents, res1.unanswerable_reason
                ):
                    trace["gate"] = "U4_llm_corroborated_unanswerable"
                    return {
                        "ans_type": "unanswerable",
                        "answer": None,
                        "reason": res1.unanswerable_reason,
                        "is_currency": spec.is_currency,
                        "month_only": spec.month_only,
                        "trace": trace,
                    }
            elif self.is_valid_execution_result(res1, spec, nq, bundle):
                cand_code = code1
                cand_res = res1
                cand_stage = "round1"

            # Vòng 2 (Self-Correction duy nhất 1 lần nếu Vòng 1 lỗi hoặc không hợp lệ)
            if cand_res is None:
                tb_feedback = (
                    res1.traceback
                    if not res1.ok
                    else f"Kết quả trả về không hợp lệ hoặc không khớp kiểu `{spec.ans_type}` ({repr(res1.value)})."
                )
                retry_msg = (
                    f"Mã gặp lỗi [{tb_feedback}]. Hãy quan sát lại Schema, Tri thức Rulebook và sửa lại đoạn code!"
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
                    if AnswerabilityGate.corroborate_llm_unanswerable(
                        nq, tctx, bundle, ents, res2.unanswerable_reason
                    ):
                        trace["gate"] = "U4_llm_retry_corroborated_unanswerable"
                        return {
                            "ans_type": "unanswerable",
                            "answer": None,
                            "reason": res2.unanswerable_reason,
                            "is_currency": spec.is_currency,
                            "month_only": spec.month_only,
                            "trace": trace,
                        }
                elif self.is_valid_execution_result(res2, spec, nq, bundle):
                    cand_code = code2
                    cand_res = res2
                    cand_stage = "round2"

            # Cổng kiểm định G6: Dual-Perspective Execution Consistency + Kadavath P(True) Calibration
            if cand_res is not None and cand_code is not None:
                agreed = True
                if self.cfg.require_dual_execution_agreement:
                    ver_prompt = self.build_verification_code_prompt(
                        nq, bundle, tctx, ents, spec, as_of_date
                    )
                    ver_messages = [
                        {"role": "system", "content": self.VERIFICATION_SYSTEM_PROMPT},
                        {"role": "user", "content": ver_prompt},
                    ]
                    ver_out = self.generate_llm_response(ver_messages)
                    ver_code = extract_code(ver_out or "")
                    ver_res = self.sandbox.execute(
                        ver_code, bundle, tctx, ents, self.cal, as_of_date
                    )
                    agreed = (
                        self.is_valid_execution_result(ver_res, spec, nq, bundle)
                        and ver_res.unanswerable_reason is None
                        and self.results_agree(cand_res.value, ver_res.value, spec)
                    )
                    trace["verification"] = {
                        "code": ver_code,
                        "ok": ver_res.ok,
                        "value": ver_res.value if ver_res.ok else None,
                        "agreed": agreed,
                    }

                if agreed:
                    p_true = self.compute_p_true_confidence(
                        nq, bundle, tctx, spec, cand_code, cand_res.value
                    )
                    trace["p_true"] = round(p_true, 4)
                    if p_true >= self.cfg.p_true_threshold:
                        trace["gate"] = f"OK_llm_{cand_stage}_verified"
                        return {
                            "ans_type": spec.ans_type,
                            "answer": cand_res.value,
                            "reason": "",
                            "is_currency": spec.is_currency,
                            "month_only": spec.month_only,
                            "trace": trace,
                        }
                    else:
                        trace["gate"] = "G6_rejected_low_p_true"
                else:
                    trace["gate"] = "G6_rejected_dual_exec_disagreement"

        # Chốt chặn an toàn cuối cùng (G4): Trả về Abstain (0 điểm) để triệt tiêu nguy cơ bị -1 điểm
        if "gate" not in trace:
            trace["gate"] = "G4_safe_abstain"
        return {
            "ans_type": "abstain",
            "answer": None,
            "reason": "",
            "is_currency": spec.is_currency,
            "month_only": spec.month_only,
            "trace": trace,
        }

    def solve_batch(
        self,
        items: List[Tuple[str, NormalizedQuestion, ShopBundle, TimeContext, EntityResult, str]],
        batch_size: int = 8,
        progress_callback: Optional[Any] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """
        Thực thi Batch Inference 4 giai đoạn trên GPU (dành cho Google Colab / Production):
        - Giai đoạn 1: AnswerabilityGate (U1-U3) + Fast-Path Deterministic Execution.
        - Giai đoạn 2: SLM Vòng 1 (Góc nhìn A với Rulebook Evidence & Atomic Helpers) theo batch.
        - Giai đoạn 3: SLM Vòng 2 (Self-Correction 1 lần kèm Traceback) cho các câu lỗi ở Vòng 1.
        - Giai đoạn 4: Kiểm định Kép (Góc nhìn B - Decomposed Verification + Kadavath P(True) Calibration).
          Chỉ những câu đạt đồng thuận thực thi (`results_agree == True`) và `P(True) >= p_true_threshold`
          mới được xuất đáp án; phần còn lại tự động `abstain` an toàn.
        """
        final_outputs: Dict[str, Dict[str, Any]] = {}
        llm_queue: List[Tuple[str, NormalizedQuestion, ShopBundle, TimeContext, EntityResult, AnswerSpec, str, Dict[str, Any]]] = []

        # Giai đoạn 1: U1-U3 Gate & Fast-Path
        for qid, nq, bundle, tctx, ents, as_of_date in items:
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
            verdict, reason, layer_id = AnswerabilityGate.check(nq, tctx, bundle.profile)
            if verdict == "unanswerable":
                trace["gate"] = layer_id
                final_outputs[qid] = {
                    "ans_type": "unanswerable",
                    "answer": None,
                    "reason": reason,
                    "is_currency": spec.is_currency,
                    "month_only": spec.month_only,
                    "trace": trace,
                }
                continue

            if tctx.mode == "none":
                trace["gate"] = "G2_missing_time"
                final_outputs[qid] = {
                    "ans_type": "abstain",
                    "answer": None,
                    "reason": "",
                    "is_currency": spec.is_currency,
                    "month_only": spec.month_only,
                    "trace": trace,
                }
                continue

            if ents.status == "not_found" and self.cfg.provider == "heuristic_only":
                trace["gate"] = "G3_entity_below_0.70"
                final_outputs[qid] = {
                    "ans_type": "abstain",
                    "answer": None,
                    "reason": "",
                    "is_currency": spec.is_currency,
                    "month_only": spec.month_only,
                    "trace": trace,
                }
                continue

            det_code = self.synthesize_deterministic_code(nq, bundle, tctx, ents, spec)
            if det_code:
                det_res = self.sandbox.execute(
                    det_code, bundle, tctx, ents, self.cal, as_of_date, fast_local=True
                )
                trace["fastpath_code"] = det_code
                trace["fastpath_ok"] = det_res.ok
                if det_res.unanswerable_reason is not None and AnswerabilityGate.corroborate_llm_unanswerable(
                    nq, tctx, bundle, ents, det_res.unanswerable_reason
                ):
                    trace["gate"] = "U2_fastpath_unanswerable"
                    final_outputs[qid] = {
                        "ans_type": "unanswerable",
                        "answer": None,
                        "reason": det_res.unanswerable_reason,
                        "is_currency": spec.is_currency,
                        "month_only": spec.month_only,
                        "trace": trace,
                    }
                    continue
                elif self.is_valid_execution_result(det_res, spec, nq, bundle):
                    trace["gate"] = "OK_fastpath"
                    final_outputs[qid] = {
                        "ans_type": spec.ans_type,
                        "answer": det_res.value,
                        "reason": "",
                        "is_currency": spec.is_currency,
                        "month_only": spec.month_only,
                        "trace": trace,
                    }
                    continue

            if self.cfg.provider.lower() == "heuristic_only":
                trace["gate"] = "G4_safe_abstain"
                final_outputs[qid] = {
                    "ans_type": "abstain",
                    "answer": None,
                    "reason": "",
                    "is_currency": spec.is_currency,
                    "month_only": spec.month_only,
                    "trace": trace,
                }
            else:
                llm_queue.append((qid, nq, bundle, tctx, ents, spec, as_of_date, trace))

        if not llm_queue:
            return final_outputs

        # Giai đoạn 2: SLM Vòng 1 (Góc nhìn A)
        retry_queue: List[Tuple[str, NormalizedQuestion, ShopBundle, TimeContext, EntityResult, AnswerSpec, str, Dict[str, Any], List[Dict[str, str]], str, str]] = []
        verify_queue: List[Tuple[str, NormalizedQuestion, ShopBundle, TimeContext, EntityResult, AnswerSpec, str, Dict[str, Any], str, SandboxResult, str]] = []

        for start_i in range(0, len(llm_queue), batch_size):
            chunk = llm_queue[start_i : start_i + batch_size]
            batch_msgs = []
            for qid, nq, bundle, tctx, ents, spec, as_of_date, _ in chunk:
                up = self.build_user_prompt(nq, bundle, tctx, ents, spec, as_of_date)
                batch_msgs.append([
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": up},
                ])
            outs = self.generate_batch_llm_responses(batch_msgs)
            for (qid, nq, bundle, tctx, ents, spec, as_of_date, trace), msgs, llm_out1 in zip(chunk, batch_msgs, outs):
                code1 = extract_code(llm_out1 or "")
                res1 = self.sandbox.execute(code1, bundle, tctx, ents, self.cal, as_of_date)
                trace["round1"] = {
                    "code": code1,
                    "ok": res1.ok,
                    "error_type": res1.error_type,
                    "traceback": res1.traceback,
                }
                if res1.unanswerable_reason is not None:
                    if AnswerabilityGate.corroborate_llm_unanswerable(
                        nq, tctx, bundle, ents, res1.unanswerable_reason
                    ):
                        trace["gate"] = "U4_llm_corroborated_unanswerable"
                        final_outputs[qid] = {
                            "ans_type": "unanswerable",
                            "answer": None,
                            "reason": res1.unanswerable_reason,
                            "is_currency": spec.is_currency,
                            "month_only": spec.month_only,
                            "trace": trace,
                        }
                    else:
                        trace["gate"] = "G4_uncorroborated_unanswerable_abstain"
                        final_outputs[qid] = {
                            "ans_type": "abstain",
                            "answer": None,
                            "reason": "",
                            "is_currency": spec.is_currency,
                            "month_only": spec.month_only,
                            "trace": trace,
                        }
                elif self.is_valid_execution_result(res1, spec, nq, bundle):
                    verify_queue.append((qid, nq, bundle, tctx, ents, spec, as_of_date, trace, code1 or "", res1, "round1"))
                else:
                    tb_fb = (
                        res1.traceback
                        if not res1.ok
                        else f"Kết quả trả về không hợp lệ hoặc không khớp kiểu `{spec.ans_type}` ({repr(res1.value)})."
                    )
                    retry_queue.append((qid, nq, bundle, tctx, ents, spec, as_of_date, trace, msgs, llm_out1 or "", tb_fb))
            if progress_callback:
                progress_callback("round1", min(start_i + batch_size, len(llm_queue)), len(llm_queue))

        # Giai đoạn 3: SLM Vòng 2 (Self-Correction)
        for start_i in range(0, len(retry_queue), batch_size):
            chunk = retry_queue[start_i : start_i + batch_size]
            batch_retry_msgs = []
            for qid, nq, bundle, tctx, ents, spec, as_of_date, trace, msgs, llm_out1, tb_fb in chunk:
                retry_msg = f"Mã gặp lỗi [{tb_fb}]. Hãy quan sát lại Schema, Tri thức Rulebook và sửa lại đoạn code!"
                batch_retry_msgs.append(
                    msgs + [
                        {"role": "assistant", "content": llm_out1},
                        {"role": "user", "content": retry_msg},
                    ]
                )
            outs2 = self.generate_batch_llm_responses(batch_retry_msgs)
            for (qid, nq, bundle, tctx, ents, spec, as_of_date, trace, _, _, tb_fb), llm_out2 in zip(chunk, outs2):
                code2 = extract_code(llm_out2 or "")
                res2 = self.sandbox.execute(code2, bundle, tctx, ents, self.cal, as_of_date)
                trace["round2"] = {
                    "code": code2,
                    "ok": res2.ok,
                    "error_type": res2.error_type,
                    "traceback": res2.traceback,
                }
                if res2.unanswerable_reason is not None:
                    if AnswerabilityGate.corroborate_llm_unanswerable(
                        nq, tctx, bundle, ents, res2.unanswerable_reason
                    ):
                        trace["gate"] = "U4_llm_retry_corroborated_unanswerable"
                        final_outputs[qid] = {
                            "ans_type": "unanswerable",
                            "answer": None,
                            "reason": res2.unanswerable_reason,
                            "is_currency": spec.is_currency,
                            "month_only": spec.month_only,
                            "trace": trace,
                        }
                    else:
                        trace["gate"] = "G4_uncorroborated_unanswerable_abstain"
                        final_outputs[qid] = {
                            "ans_type": "abstain",
                            "answer": None,
                            "reason": "",
                            "is_currency": spec.is_currency,
                            "month_only": spec.month_only,
                            "trace": trace,
                        }
                elif self.is_valid_execution_result(res2, spec, nq, bundle):
                    verify_queue.append((qid, nq, bundle, tctx, ents, spec, as_of_date, trace, code2 or "", res2, "round2"))
                else:
                    trace["gate"] = "G4_safe_abstain"
                    final_outputs[qid] = {
                        "ans_type": "abstain",
                        "answer": None,
                        "reason": "",
                        "is_currency": spec.is_currency,
                        "month_only": spec.month_only,
                        "trace": trace,
                    }
            if progress_callback:
                progress_callback("round2", min(start_i + batch_size, len(retry_queue)), len(retry_queue))

        # Giai đoạn 4: Kiểm định Kép (Dual-Perspective Execution Verification + Kadavath P(True) Calibration)
        agreed_queue: List[Tuple[str, NormalizedQuestion, ShopBundle, TimeContext, EntityResult, AnswerSpec, str, Dict[str, Any], str, SandboxResult, str]] = []
        if self.cfg.require_dual_execution_agreement and verify_queue:
            for start_i in range(0, len(verify_queue), batch_size):
                chunk = verify_queue[start_i : start_i + batch_size]
                ver_batch_msgs = []
                for qid, nq, bundle, tctx, ents, spec, as_of_date, trace, cand_code, cand_res, stage in chunk:
                    vp = self.build_verification_code_prompt(nq, bundle, tctx, ents, spec, as_of_date)
                    ver_batch_msgs.append([
                        {"role": "system", "content": self.VERIFICATION_SYSTEM_PROMPT},
                        {"role": "user", "content": vp},
                    ])
                ver_outs = self.generate_batch_llm_responses(ver_batch_msgs)
                for (qid, nq, bundle, tctx, ents, spec, as_of_date, trace, cand_code, cand_res, stage), ver_out in zip(chunk, ver_outs):
                    ver_code = extract_code(ver_out or "")
                    ver_res = self.sandbox.execute(ver_code, bundle, tctx, ents, self.cal, as_of_date)
                    agreed = (
                        self.is_valid_execution_result(ver_res, spec, nq, bundle)
                        and ver_res.unanswerable_reason is None
                        and self.results_agree(cand_res.value, ver_res.value, spec)
                    )
                    trace["verification"] = {
                        "code": ver_code,
                        "ok": ver_res.ok,
                        "value": ver_res.value if ver_res.ok else None,
                        "agreed": agreed,
                    }
                    if agreed:
                        agreed_queue.append((qid, nq, bundle, tctx, ents, spec, as_of_date, trace, cand_code, cand_res, stage))
                    else:
                        trace["gate"] = "G6_rejected_dual_exec_disagreement"
                        final_outputs[qid] = {
                            "ans_type": "abstain",
                            "answer": None,
                            "reason": "",
                            "is_currency": spec.is_currency,
                            "month_only": spec.month_only,
                            "trace": trace,
                        }
                if progress_callback:
                    progress_callback("verify", min(start_i + batch_size, len(verify_queue)), len(verify_queue))
        else:
            agreed_queue = verify_queue

        # Kiểm định xác suất P(True) (Kadavath Calibration Gate)
        for start_i in range(0, len(agreed_queue), batch_size):
            chunk = agreed_queue[start_i : start_i + batch_size]
            p_true_inputs = [
                (nq, bundle, tctx, spec, cand_code, cand_res.value)
                for (qid, nq, bundle, tctx, ents, spec, as_of_date, trace, cand_code, cand_res, stage) in chunk
            ]
            p_trues = self.compute_batch_p_true_confidence(p_true_inputs)
            for (qid, nq, bundle, tctx, ents, spec, as_of_date, trace, cand_code, cand_res, stage), p_true in zip(chunk, p_trues):
                trace["p_true"] = round(p_true, 4)
                if p_true >= self.cfg.p_true_threshold:
                    trace["gate"] = f"OK_llm_{stage}_verified"
                    final_outputs[qid] = {
                        "ans_type": spec.ans_type,
                        "answer": cand_res.value,
                        "reason": "",
                        "is_currency": spec.is_currency,
                        "month_only": spec.month_only,
                        "trace": trace,
                    }
                else:
                    trace["gate"] = "G6_rejected_low_p_true"
                    final_outputs[qid] = {
                        "ans_type": "abstain",
                        "answer": None,
                        "reason": "",
                        "is_currency": spec.is_currency,
                        "month_only": spec.month_only,
                        "trace": trace,
                    }

        return final_outputs

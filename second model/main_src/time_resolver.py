import calendar
import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .preprocessor import NormalizedQuestion, strip_accents


@dataclass
class Period:
    start: Optional[str]
    end: Optional[str]
    label: str = ""


@dataclass
class TimeContext:
    mode: str  # "period" | "snapshot" | "multi" | "none"
    primary: Optional[Period]
    compare: Optional[Period] = None
    periods: List[Period] = field(default_factory=list)
    shift: Optional[str] = None
    compare_shift: Optional[str] = None
    rule_id: str = "none"
    confidence: float = 0.0
    has_time_cue: bool = False
    as_of: str = ""

    def to_legacy_dict(self) -> Dict[str, Any]:
        """Chuyển đổi sang định dạng dict tương thích 100% với code cũ và Sandbox T."""
        start_d = self.primary.start if self.primary else self.as_of
        end_d = self.primary.end if self.primary else self.as_of
        cmp_start = self.compare.start if self.compare else None
        cmp_end = self.compare.end if self.compare else None
        return {
            "as_of": self.as_of,
            "mode": self.mode,
            "start_date": start_d,
            "end_date": end_d,
            "start": start_d,
            "end": end_d,
            "compare_start_date": cmp_start,
            "compare_end_date": cmp_end,
            "cmp_start": cmp_start,
            "cmp_end": cmp_end,
            "shift": self.shift,
            "cmp_shift": self.compare_shift,
            "raw_type": self.rule_id,
            "confidence": self.confidence,
            "has_time_cue": self.has_time_cue,
            "periods": [
                {
                    "start_date": p.start,
                    "end_date": p.end,
                    "start": p.start,
                    "end": p.end,
                    "month_label": p.label,
                    "label": p.label,
                }
                for p in self.periods
            ],
        }


class CalendarResolver:
    """
    Kế thừa chính xác từ bài làm nộp thử/datamate/calendar_resolver.py.
    Đã vá lỗi: kiểm tra cột lunar_leap=true để tháng nhuận không ghi đè tháng thường.
    """

    def __init__(self, calendar_path: Optional[str] = None) -> None:
        self.date_to_lunar: Dict[str, Tuple[int, int, int]] = {}
        self.lunar_to_date: Dict[Tuple[int, int, int], str] = {}
        self.date_to_info: Dict[str, Dict[str, Any]] = {}
        self.min_date: Optional[str] = None
        self.max_date: Optional[str] = None
        if calendar_path:
            self.load_calendar(calendar_path)

    def load_calendar(self, calendar_path: str) -> None:
        with open(calendar_path, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                d_str = row["date"].strip()
                l_day = int(row["lunar_day"])
                l_month = int(row["lunar_month"])
                l_year = int(row["lunar_year"])
                w_day = int(row["weekday"])
                is_leap = str(row.get("lunar_leap", "false")).strip().lower() == "true"
                holiday = (row.get("holiday") or "").strip()

                self.date_to_lunar[d_str] = (l_day, l_month, l_year)
                if not is_leap:
                    self.lunar_to_date[(l_year, l_month, l_day)] = d_str
                self.date_to_info[d_str] = {
                    "weekday": w_day,
                    "lunar": (l_day, l_month, l_year),
                    "leap": is_leap,
                    "holiday": holiday,
                }
        if self.date_to_info:
            sorted_dates = sorted(self.date_to_info.keys())
            self.min_date = sorted_dates[0]
            self.max_date = sorted_dates[-1]

    def get_tet_range(
        self, tet_year: int
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        """
        Rulebook §2 (Mục 36):
        Tết năm Y, dịp Tết, mùa Tết: từ ngày dương của 23 tháng Chạp (năm âm Y - 1)
        đến hết ngày dương của mùng 10 tháng Giêng (năm âm Y).
        So với trước Tết = cùng số ngày ngay trước 23 tháng Chạp.
        """
        start_date = self.lunar_to_date.get((tet_year - 1, 12, 23))
        end_date = self.lunar_to_date.get((tet_year, 1, 10))

        if not start_date or not end_date:
            return None, None, None, None

        d_start = datetime.strptime(start_date, "%Y-%m-%d").date()
        d_end = datetime.strptime(end_date, "%Y-%m-%d").date()
        num_days = (d_end - d_start).days + 1

        d_before_end = d_start - timedelta(days=1)
        d_before_start = d_before_end - timedelta(days=num_days - 1)

        before_start_date = d_before_start.strftime("%Y-%m-%d")
        before_end_date = d_before_end.strftime("%Y-%m-%d")

        return start_date, end_date, before_start_date, before_end_date

    def get_before_tet_n_days(
        self, tet_year: int, n_days: int
    ) -> Tuple[Optional[str], Optional[str]]:
        """Lấy khoảng N ngày ngay trước 23 tháng Chạp của Tết năm Y."""
        start_date, _, _, _ = self.get_tet_range(tet_year)
        if not start_date:
            return None, None
        d_start = datetime.strptime(start_date, "%Y-%m-%d").date()
        b_end = d_start - timedelta(days=1)
        b_start = b_end - timedelta(days=n_days - 1)
        return b_start.strftime("%Y-%m-%d"), b_end.strftime("%Y-%m-%d")

    def count_weekday(self, start_str: str, end_str: str, iso_weekday: int) -> int:
        """
        Rulebook §2: Trung bình mỗi ngày thứ X của kỳ -> chia cho số ngày thứ X trong kỳ,
        kể cả ngày không có đơn. (iso_weekday: 1=Thứ Hai ... 7=Chủ Nhật).
        """
        d = datetime.strptime(start_str, "%Y-%m-%d").date()
        e = datetime.strptime(end_str, "%Y-%m-%d").date()
        cnt = 0
        while d <= e:
            if d.isoweekday() == iso_weekday:
                cnt += 1
            d += timedelta(days=1)
        return cnt


class TimeResolver:
    """
    Module Phân giải Thời gian Tất định (Bước 2.1 trong Kiến trúc):
    Kế thừa toàn bộ logic của TimeParser cũ và mở rộng theo Blueprint §3.2:
    - Chạy đồng thời trên chuỗi có dấu và chuỗi ASCII không dấu (xử lý 262 câu không dấu).
    - Hỗ trợ định dạng M/YY (ví dụ: '8/26', 'tháng 10/25', '3/25, 4/25 và 5/25').
    - Hỗ trợ Tết đơn, Tết so với trước Tết, và Tết năm nay so với Tết năm ngoái.
    - Hỗ trợ chế độ Snapshot ('tính đến hết ngày X', 'cuối ngày hôm nay').
    - Hỗ trợ Multi-period (nhiều tháng liên tiếp hoặc danh sách tháng).
    """

    def __init__(self, calendar_resolver: Optional[CalendarResolver] = None) -> None:
        self.cal = calendar_resolver

    @staticmethod
    def _year_for_month(m: int, as_of: date) -> int:
        return as_of.year - 1 if m > as_of.month else as_of.year

    @staticmethod
    def _year_for_quarter(q: int, as_of: date) -> int:
        curr_q = (as_of.month - 1) // 3 + 1
        return as_of.year - 1 if q > curr_q else as_of.year

    @staticmethod
    def _year_for_day_month(d: int, m: int, as_of: date) -> int:
        return as_of.year if (m, d) <= (as_of.month, as_of.day) else as_of.year - 1

    def detect_shift(self, ascii_text: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Rulebook §2: Ca sáng 06:00-13:59, ca chiều 14:00-21:59.
        So ca chiều với ca sáng: mốc là ca sáng.
        """
        has_morning = "ca sang" in ascii_text
        has_afternoon = "ca chieu" in ascii_text
        if has_morning and has_afternoon:
            # Nếu câu hỏi so sánh ca chiều với ca sáng
            m_pos = ascii_text.find("ca sang")
            a_pos = ascii_text.find("ca chieu")
            if a_pos < m_pos:
                return "afternoon", "morning"
            return "morning", "afternoon"
        if has_morning:
            return "morning", None
        if has_afternoon:
            return "afternoon", None
        return None, None

    def has_time_cue(self, ascii_text: str) -> bool:
        cues = [
            r"\bngay\b",
            r"\bhom\b",
            r"\btuan\b",
            r"\bthang\b",
            r"\bquy\b",
            r"\bnam\b",
            r"\btet\b",
            r"\bgan nhat\b",
            r"\bgan day\b",
            r"\bdau nam\b",
            r"\bdau thang\b",
            r"\bden het\b",
            r"\btinh den\b",
            r"\bcuoi ngay\b",
            r"\bca sang\b",
            r"\bca chieu\b",
            r"\b\d{1,2}[/-]\d{1,4}\b",
        ]
        return any(re.search(p, ascii_text) for p in cues)

    def resolve_month(self, m: int, y: Optional[int], as_of: date) -> Period:
        if y is None:
            year = self._year_for_month(m, as_of)
        else:
            year = y + 2000 if y < 100 else y
        _, last_day = calendar.monthrange(year, m)
        return Period(
            start=f"{year:04d}-{m:02d}-01",
            end=f"{year:04d}-{m:02d}-{last_day:02d}",
            label=f"{year:04d}-{m:02d}",
        )

    def resolve_quarter(self, q: int, y: Optional[int], as_of: date) -> Period:
        if y is None:
            year = self._year_for_quarter(q, as_of)
        else:
            year = y + 2000 if y < 100 else y
        start_m = (q - 1) * 3 + 1
        end_m = q * 3
        _, last_day = calendar.monthrange(year, end_m)
        return Period(
            start=f"{year:04d}-{start_m:02d}-01",
            end=f"{year:04d}-{end_m:02d}-{last_day:02d}",
            label=f"{year:04d}-Q{q}",
        )

    def resolve(
        self, nq_or_text: Any, as_of_date_str: str
    ) -> TimeContext:
        """
        Phân giải thời gian từ NormalizedQuestion hoặc chuỗi câu hỏi.
        Đảm bảo không bao giờ ném exception.
        """
        try:
            if isinstance(nq_or_text, NormalizedQuestion):
                text_lower = nq_or_text.text.lower()
                ascii_text = nq_or_text.ascii.lower()
            else:
                text_lower = str(nq_or_text).lower().strip()
                ascii_text = strip_accents(text_lower).lower().strip()

            as_of = datetime.strptime(as_of_date_str.strip(), "%Y-%m-%d").date()
            shift, cmp_shift = self.detect_shift(ascii_text)
            cue = self.has_time_cue(ascii_text)

            # 0a. Multi-month: "N tháng liền nhau kết thúc ở tháng M/Y"
            m_mlist = re.search(
                r"(\d+)\s*thang\s*lien\s*nhau\s*ket\s*thuc\s*o\s*thang\s*(\d{1,2})[/-](\d{2,4})",
                ascii_text,
            )
            if m_mlist:
                n_m = int(m_mlist.group(1))
                end_m = int(m_mlist.group(2))
                end_y = int(m_mlist.group(3))
                if end_y < 100:
                    end_y += 2000
                periods: List[Period] = []
                cy, cm = end_y, end_m
                for _ in range(min(n_m, 24)):
                    periods.append(self.resolve_month(cm, cy, as_of))
                    cm -= 1
                    if cm < 1:
                        cm = 12
                        cy -= 1
                if "tu thang moi" not in ascii_text:
                    periods.reverse()
                return TimeContext(
                    mode="multi",
                    primary=Period(periods[0].start, periods[-1].end, "multi_months"),
                    periods=periods,
                    shift=shift,
                    compare_shift=cmp_shift,
                    rule_id="R2c_multi_consecutive",
                    confidence=1.0,
                    has_time_cue=True,
                    as_of=as_of_date_str,
                )

            # 0b. Multi-month range: "từ tháng M1/Y1 đến/tới/qua tháng M2/Y2"
            m_mrange = re.search(
                r"tu\s*thang\s*(\d{1,2})[/-](\d{2,4})\s*(?:den|toi|qua)\s*thang\s*(\d{1,2})[/-](\d{2,4})",
                ascii_text,
            )
            if m_mrange:
                m1, y1 = int(m_mrange.group(1)), int(m_mrange.group(2))
                m2, y2 = int(m_mrange.group(3)), int(m_mrange.group(4))
                if y1 < 100:
                    y1 += 2000
                if y2 < 100:
                    y2 += 2000
                periods = []
                cy, cm = y1, m1
                while (cy, cm) <= (y2, m2) and len(periods) < 24:
                    periods.append(self.resolve_month(cm, cy, as_of))
                    cm += 1
                    if cm > 12:
                        cm = 1
                        cy += 1
                if periods:
                    return TimeContext(
                        mode="multi",
                        primary=Period(periods[0].start, periods[-1].end, "month_range"),
                        periods=periods,
                        shift=shift,
                        compare_shift=cmp_shift,
                        rule_id="R2b_month_range",
                        confidence=1.0,
                        has_time_cue=True,
                        as_of=as_of_date_str,
                    )

            # 0c. Multi-month explicit list: "3 tháng 1/2026, 2/2026 và 3/2026" hoặc "So sánh 3/25, 4/25 và 5/25"
            m_explicit_months = re.findall(
                r"(?<!\d[/-])\b(1[0-2]|0?[1-9])[/-](202\d|2[4-7])\b(?![/-]\d)",
                ascii_text,
            )
            if len(m_explicit_months) >= 3 or (
                len(m_explicit_months) >= 2
                and any(
                    k in ascii_text
                    for k in ["thang nao", "xet ", "so sanh", "qua cac thang", "tang lien tuc"]
                )
            ):
                periods = [
                    self.resolve_month(int(mm), int(yy), as_of)
                    for mm, yy in m_explicit_months
                ]
                return TimeContext(
                    mode="multi",
                    primary=Period(periods[0].start, periods[-1].end, "month_list"),
                    periods=periods,
                    shift=shift,
                    compare_shift=cmp_shift,
                    rule_id="R2b_month_list",
                    confidence=1.0,
                    has_time_cue=True,
                    as_of=as_of_date_str,
                )

            # 1. R1: Tết Nguyên Đán (loại trừ Tết Dương lịch / Tết Tây)
            if (
                re.search(r"\btet\b", ascii_text)
                and "tet duong lich" not in ascii_text
                and "tet tay" not in ascii_text
                and self.cal
            ):
                # Kiểm tra so sánh Tết năm nay vs Tết năm ngoái/trước
                if re.search(
                    r"tet\s+nam\s+nay.*(?:so\s+voi|voi).*tet\s+nam\s+(?:ngoai|truoc|trc)",
                    ascii_text,
                ):
                    y1 = as_of.year
                    y2 = as_of.year - 1
                    s1, e1, _, _ = self.cal.get_tet_range(y1)
                    s2, e2, _, _ = self.cal.get_tet_range(y2)
                    if s1 and e1 and s2 and e2:
                        return TimeContext(
                            mode="period",
                            primary=Period(s1, e1, f"tet_{y1}"),
                            compare=Period(s2, e2, f"tet_{y2}"),
                            shift=shift,
                            compare_shift=cmp_shift,
                            rule_id="R1_tet_vs_tet",
                            confidence=1.0,
                            has_time_cue=True,
                            as_of=as_of_date_str,
                        )

                # Tìm năm của Tết
                m_tet_yr = re.search(
                    r"tet(?:\s+am\s+lich|\s+nguyen\s+dan)?(?:\s+nam)?\s*(202\d)",
                    ascii_text,
                )
                if m_tet_yr:
                    tet_year = int(m_tet_yr.group(1))
                elif "tet nam ngoai" in ascii_text or "tet nam truoc" in ascii_text:
                    tet_year = as_of.year - 1
                elif "tet nam nay" in ascii_text:
                    tet_year = as_of.year
                else:
                    # Nếu không nói năm: lấy năm nay nếu cửa sổ Tết năm nay đã bắt đầu (<= as_of), ngược lại năm ngoái
                    s_curr, _, _, _ = self.cal.get_tet_range(as_of.year)
                    if s_curr and s_curr <= as_of_date_str:
                        tet_year = as_of.year
                    else:
                        tet_year = as_of.year - 1

                s_tet, e_tet, b_start, b_end = self.cal.get_tet_range(tet_year)
                if s_tet and e_tet:
                    cmp_period = None
                    m_n_before = re.search(
                        r"(\d+)\s*ngay\s*(?:ngay\s*)?(?:truoc|trc)\s*tet", ascii_text
                    )
                    if m_n_before:
                        n_b = int(m_n_before.group(1))
                        bs, be = self.cal.get_before_tet_n_days(tet_year, n_b)
                        if bs and be:
                            cmp_period = Period(bs, be, f"before_tet_{n_b}d")
                    elif "truoc tet" in ascii_text or "trc tet" in ascii_text:
                        cmp_period = Period(b_start, b_end, "before_tet")

                    return TimeContext(
                        mode="period",
                        primary=Period(s_tet, e_tet, f"tet_{tet_year}"),
                        compare=cmp_period,
                        shift=shift,
                        compare_shift=cmp_shift,
                        rule_id="R1_tet_compare" if cmp_period else "R1_tet",
                        confidence=1.0,
                        has_time_cue=True,
                        as_of=as_of_date_str,
                    )

            # 2. R2: Khoảng ngày tường minh "từ (ngày) D1/M1(/Y1) đến (hết) (ngày) D2/M2(/Y2)"
            m_range = re.search(
                r"tu\s*(?:ngay\s*)?(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\s*(?:den|toi|-)\s*(?:het\s*)?(?:ngay\s*)?(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?",
                ascii_text,
            )
            if m_range:
                d1, m1, y1_s = (
                    int(m_range.group(1)),
                    int(m_range.group(2)),
                    m_range.group(3),
                )
                d2, m2, y2_s = (
                    int(m_range.group(4)),
                    int(m_range.group(5)),
                    m_range.group(6),
                )
                if y2_s:
                    y2 = int(y2_s)
                    if y2 < 100:
                        y2 += 2000
                else:
                    y2 = self._year_for_day_month(d2, m2, as_of)

                if y1_s:
                    y1 = int(y1_s)
                    if y1 < 100:
                        y1 += 2000
                else:
                    y1 = y2 if (m1, d1) <= (m2, d2) else y2 - 1

                start_d = f"{y1:04d}-{m1:02d}-{d1:02d}"
                end_d = f"{y2:04d}-{m2:02d}-{d2:02d}"
                return TimeContext(
                    mode="period",
                    primary=Period(start_d, end_d, "date_range"),
                    shift=shift,
                    compare_shift=cmp_shift,
                    rule_id="R2_date_range",
                    confidence=1.0,
                    has_time_cue=True,
                    as_of=as_of_date_str,
                )

            # 2b. Hiệu ứng tăng giá: "N ngày kể từ (ngày) D/M/Y ... với N ngày ngay trước"
            m_price_eff = re.search(
                r"(\d+)\s*ngay\s*ke\s*tu\s*(?:ngay\s*)?(\d{1,2})[/-](\d{1,2})[/-](\d{4})",
                ascii_text,
            )
            if not m_price_eff:
                m_price_eff_alt = re.search(
                    r"(?:ngay\s*)?(\d{1,2})[/-](\d{1,2})[/-](\d{4}).*?(\d+)\s*ngay\s*ke\s*tu\s*ngay\s*do",
                    ascii_text,
                )
                if m_price_eff_alt:
                    dd = int(m_price_eff_alt.group(1))
                    mm = int(m_price_eff_alt.group(2))
                    yy = int(m_price_eff_alt.group(3))
                    nn = int(m_price_eff_alt.group(4))
                    d_pivot = date(yy, mm, dd)
                    s_after = d_pivot
                    e_after = d_pivot + timedelta(days=nn - 1)
                    e_before = d_pivot - timedelta(days=1)
                    s_before = e_before - timedelta(days=nn - 1)
                    return TimeContext(
                        mode="period",
                        primary=Period(s_after.isoformat(), e_after.isoformat(), "after_price_change"),
                        compare=Period(s_before.isoformat(), e_before.isoformat(), "before_price_change"),
                        shift=shift,
                        compare_shift=cmp_shift,
                        rule_id="R2_price_effect",
                        confidence=1.0,
                        has_time_cue=True,
                        as_of=as_of_date_str,
                    )
            else:
                nn = int(m_price_eff.group(1))
                dd, mm, yy = (
                    int(m_price_eff.group(2)),
                    int(m_price_eff.group(3)),
                    int(m_price_eff.group(4)),
                )
                d_pivot = date(yy, mm, dd)
                s_after = d_pivot
                e_after = d_pivot + timedelta(days=nn - 1)
                e_before = d_pivot - timedelta(days=1)
                s_before = e_before - timedelta(days=nn - 1)
                return TimeContext(
                    mode="period",
                    primary=Period(s_after.isoformat(), e_after.isoformat(), "after_price_change"),
                    compare=Period(s_before.isoformat(), e_before.isoformat(), "before_price_change"),
                    shift=shift,
                    compare_shift=cmp_shift,
                    rule_id="R2_price_effect",
                    confidence=1.0,
                    has_time_cue=True,
                    as_of=as_of_date_str,
                )

            # 3. R3: "từ đầu năm", "từ 1/1 năm nay", "từ 01/01 đến hết hôm nay"
            if any(
                k in ascii_text
                for k in [
                    "dau nam",
                    "1/1 nam nay",
                    "01/01 nam nay",
                    "tu ngay 1/1",
                    "tu ngay 01/01",
                    "tinh tu ngay 1/1",
                    "tinh tu ngay 01/01",
                ]
            ):
                start_d = f"{as_of.year:04d}-01-01"
                return TimeContext(
                    mode="period",
                    primary=Period(start_d, as_of.isoformat(), "ytd"),
                    shift=shift,
                    compare_shift=cmp_shift,
                    rule_id="R3_ytd",
                    confidence=1.0,
                    has_time_cue=True,
                    as_of=as_of_date_str,
                )

            # 4. R3b: "từ đầu tháng này đến hết hôm nay" / "tháng này tới hôm nay"
            if (
                "dau thang nay" in ascii_text
                or "thang nay toi hom nay" in ascii_text
                or "thang nay den het hom nay" in ascii_text
            ):
                return self._build_this_month(as_of, as_of_date_str, shift, cmp_shift)

            # 5. R4: Snapshot "tính đến hết ngày X", "đến hết ngày X", "cuối ngày X / hôm nay", "hết hôm nay"
            # (Áp dụng cho câu hỏi tồn kho / công nợ / số còn lại)
            if (
                any(
                    k in ascii_text
                    for k in [
                        "tinh den het",
                        "tinh den hom nay",
                        "den het ngay",
                        "den het hom nay",
                        "cuoi ngay",
                        "het hom nay",
                    ]
                )
                and "tu dau" not in ascii_text
                and "tu ngay" not in ascii_text
            ):
                m_snap_d = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", ascii_text)
                if m_snap_d:
                    dd, mm, yy = (
                        int(m_snap_d.group(1)),
                        int(m_snap_d.group(2)),
                        int(m_snap_d.group(3)),
                    )
                    snap_end = f"{yy:04d}-{mm:02d}-{dd:02d}"
                else:
                    snap_end = as_of.isoformat()
                return TimeContext(
                    mode="snapshot",
                    primary=Period(None, snap_end, f"snapshot_{snap_end}"),
                    shift=shift,
                    compare_shift=cmp_shift,
                    rule_id="R4_snapshot",
                    confidence=1.0,
                    has_time_cue=True,
                    as_of=as_of_date_str,
                )

            # 6. R5: "hôm nay" (trong ngày hôm nay)
            if (
                "hom nay" in ascii_text
                and "tu dau" not in ascii_text
                and "tinh den" not in ascii_text
                and "den het" not in ascii_text
                and "thang nay" not in ascii_text
                and "tuan nay" not in ascii_text
                and "nam nay" not in ascii_text
                and "ngay gan" not in ascii_text
            ):
                d_str = as_of.isoformat()
                prev_str = (as_of - timedelta(days=1)).isoformat()
                return TimeContext(
                    mode="period",
                    primary=Period(d_str, d_str, "today"),
                    compare=Period(prev_str, prev_str, "yesterday"),
                    shift=shift,
                    compare_shift=cmp_shift,
                    rule_id="R5_today",
                    confidence=1.0,
                    has_time_cue=True,
                    as_of=as_of_date_str,
                )

            # 7. R6: "hôm qua" / "hôm kia"
            if "hom qua" in ascii_text:
                d_str = (as_of - timedelta(days=1)).isoformat()
                prev_str = (as_of - timedelta(days=2)).isoformat()
                return TimeContext(
                    mode="period",
                    primary=Period(d_str, d_str, "yesterday"),
                    compare=Period(prev_str, prev_str, "day_before_yesterday"),
                    shift=shift,
                    compare_shift=cmp_shift,
                    rule_id="R6_yesterday",
                    confidence=1.0,
                    has_time_cue=True,
                    as_of=as_of_date_str,
                )

            # 8. R7: "N ngày gần nhất / gần đây" (Rulebook §2: N ngày liền nhau kể cả hôm nay)
            m_ndays = re.search(r"(\d+)\s*ngay\s*gan\s*(?:nhat|day)", ascii_text)
            if m_ndays:
                n = int(m_ndays.group(1))
                start_d = as_of - timedelta(days=n - 1)
                cmp_end = start_d - timedelta(days=1)
                cmp_start = cmp_end - timedelta(days=n - 1)
                return TimeContext(
                    mode="period",
                    primary=Period(start_d.isoformat(), as_of.isoformat(), f"last_{n}_days"),
                    compare=Period(cmp_start.isoformat(), cmp_end.isoformat(), f"prev_{n}_days"),
                    shift=shift,
                    compare_shift=cmp_shift,
                    rule_id="R7_n_days",
                    confidence=1.0,
                    has_time_cue=True,
                    as_of=as_of_date_str,
                )

            # 9. R8: Ngày cụ thể DD/MM/YYYY
            m_date = re.search(r"(?:ngay\s*)?(\d{1,2})[/-](\d{1,2})[/-](\d{4})", ascii_text)
            if m_date:
                day, month, year = (
                    int(m_date.group(1)),
                    int(m_date.group(2)),
                    int(m_date.group(3)),
                )
                d_str = f"{year:04d}-{month:02d}-{day:02d}"
                return TimeContext(
                    mode="period",
                    primary=Period(d_str, d_str, d_str),
                    shift=shift,
                    compare_shift=cmp_shift,
                    rule_id="R8_exact_date",
                    confidence=1.0,
                    has_time_cue=True,
                    as_of=as_of_date_str,
                )

            # 9b. R8b: Ngày cụ thể không ghi năm "ngày DD/MM" hoặc "hôm DD/MM"
            m_date_short = re.search(
                r"(?:ngay|hom)\s+(\d{1,2})[/-](\d{1,2})(?![/-]\d)", ascii_text
            )
            if m_date_short:
                day, month = int(m_date_short.group(1)), int(m_date_short.group(2))
                if 1 <= day <= 31 and 1 <= month <= 12:
                    year = self._year_for_day_month(day, month, as_of)
                    d_str = f"{year:04d}-{month:02d}-{day:02d}"
                    return TimeContext(
                        mode="period",
                        primary=Period(d_str, d_str, d_str),
                        shift=shift,
                        compare_shift=cmp_shift,
                        rule_id="R8b_exact_date_short",
                        confidence=1.0,
                        has_time_cue=True,
                        as_of=as_of_date_str,
                    )

            # 10. R9: "tuần này" (từ thứ Hai của tuần chứa hôm nay đến hết hôm nay)
            if "tuan nay" in ascii_text:
                start_d = as_of - timedelta(days=as_of.weekday())
                n_days = (as_of - start_d).days + 1
                cmp_start = start_d - timedelta(days=7)
                cmp_end = cmp_start + timedelta(days=n_days - 1)
                return TimeContext(
                    mode="period",
                    primary=Period(start_d.isoformat(), as_of.isoformat(), "this_week"),
                    compare=Period(cmp_start.isoformat(), cmp_end.isoformat(), "last_week_same_days"),
                    shift=shift,
                    compare_shift=cmp_shift,
                    rule_id="R9_this_week",
                    confidence=1.0,
                    has_time_cue=True,
                    as_of=as_of_date_str,
                )

            # 11. R10: "tuần trước", "tuần rồi" (cả tuần liền trước, Thứ Hai đến Chủ Nhật)
            if "tuan truoc" in ascii_text or "tuan roi" in ascii_text:
                monday_this_week = as_of - timedelta(days=as_of.weekday())
                sunday_last_week = monday_this_week - timedelta(days=1)
                monday_last_week = sunday_last_week - timedelta(days=6)
                cmp_sunday = monday_last_week - timedelta(days=1)
                cmp_monday = cmp_sunday - timedelta(days=6)
                return TimeContext(
                    mode="period",
                    primary=Period(
                        monday_last_week.isoformat(),
                        sunday_last_week.isoformat(),
                        "last_week",
                    ),
                    compare=Period(
                        cmp_monday.isoformat(), cmp_sunday.isoformat(), "prev_2_week"
                    ),
                    shift=shift,
                    compare_shift=cmp_shift,
                    rule_id="R10_last_week",
                    confidence=1.0,
                    has_time_cue=True,
                    as_of=as_of_date_str,
                )

            # 12. R11: Quý Q(/Y) — Kiểm tra TRƯỚC Tháng để "quý 2/2026" không bị hiểu nhầm là tháng 2/2026
            m_q = re.search(
                r"\bquy\s*(\d)(?:[\s/-]+(?:nam\s*)?(\d{4}))?", ascii_text
            )
            if m_q:
                q = int(m_q.group(1))
                if 1 <= q <= 4:
                    q_yr = int(m_q.group(2)) if m_q.group(2) else None
                    prim = self.resolve_quarter(q, q_yr, as_of)
                    eff_yr = int(prim.start[:4])
                    prev_q = q - 1 if q > 1 else 4
                    prev_y = eff_yr if q > 1 else eff_yr - 1
                    cmp_p = self.resolve_quarter(prev_q, prev_y, as_of)
                    return TimeContext(
                        mode="period",
                        primary=prim,
                        compare=cmp_p,
                        shift=shift,
                        compare_shift=cmp_shift,
                        rule_id="R11_quarter",
                        confidence=1.0,
                        has_time_cue=True,
                        as_of=as_of_date_str,
                    )

            # 13. R12: "tháng này" (Rulebook §2: từ ngày 1 đến hết hôm nay; so với tháng trước lấy cùng số ngày)
            if "thang nay" in ascii_text:
                return self._build_this_month(as_of, as_of_date_str, shift, cmp_shift)

            # 14. R12b: "tháng trước" / "tháng rồi" (khi không đi sau một tháng cụ thể)
            if (
                ("thang truoc" in ascii_text or "thang roi" in ascii_text)
                and not re.search(r"thang\s*\d", ascii_text)
                and "so voi thang truoc" not in ascii_text
            ):
                prev_year = as_of.year if as_of.month > 1 else as_of.year - 1
                prev_month = as_of.month - 1 if as_of.month > 1 else 12
                prim = self.resolve_month(prev_month, prev_year, as_of)

                pp_year = prev_year if prev_month > 1 else prev_year - 1
                pp_month = prev_month - 1 if prev_month > 1 else 12
                cmp_p = self.resolve_month(pp_month, pp_year, as_of)
                return TimeContext(
                    mode="period",
                    primary=prim,
                    compare=cmp_p,
                    shift=shift,
                    compare_shift=cmp_shift,
                    rule_id="R12b_last_month",
                    confidence=1.0,
                    has_time_cue=True,
                    as_of=as_of_date_str,
                )

            # 15. R13 & R13b: Tháng M/YYYY, Tháng M/YY, hoặc M/YY (ví dụ "8/26", "10/25")
            all_months = list(
                re.finditer(
                    r"thang\s*(\d{1,2})(?:[\s/-]+(?:nam\s*)?(\d{2,4}))?",
                    ascii_text,
                )
            )
            if not all_months:
                all_months = list(
                    re.finditer(
                        r"(?<![\d/-])\b(1[0-2]|0?[1-9])[/-](202\d|2[4-7])\b(?![/-]\d)",
                        ascii_text,
                    )
                )

            if all_months:
                m_obj = all_months[0]
                m_val = int(m_obj.group(1))
                y_raw = int(m_obj.group(2)) if m_obj.group(2) else None
                if 1 <= m_val <= 12:
                    prim = self.resolve_month(m_val, y_raw, as_of)
                    eff_year = int(prim.start[:4])

                    # Xác định kỳ so sánh
                    if len(all_months) >= 2 and any(
                        k in ascii_text for k in ["so voi", "voi thang", "so thang"]
                    ):
                        m2_obj = all_months[1]
                        m2_val = int(m2_obj.group(1))
                        y2_raw = int(m2_obj.group(2)) if m2_obj.group(2) else eff_year
                        cmp_p = (
                            self.resolve_month(m2_val, y2_raw, as_of)
                            if 1 <= m2_val <= 12
                            else None
                        )
                    elif any(
                        k in ascii_text
                        for k in [
                            "cung ky nam truoc",
                            "cung ky nam ngoai",
                            "so voi nam truoc",
                            "so voi nam ngoai",
                            "nam ngoai",
                        ]
                    ):
                        cmp_p = self.resolve_month(m_val, eff_year - 1, as_of)
                    else:
                        prev_year = eff_year if m_val > 1 else eff_year - 1
                        prev_month = m_val - 1 if m_val > 1 else 12
                        cmp_p = self.resolve_month(prev_month, prev_year, as_of)

                    return TimeContext(
                        mode="period",
                        primary=prim,
                        compare=cmp_p,
                        shift=shift,
                        compare_shift=cmp_shift,
                        rule_id="R13_specific_month",
                        confidence=1.0,
                        has_time_cue=True,
                        as_of=as_of_date_str,
                    )

            # 16. R14: Năm Y / năm nay / năm ngoái
            m_yr_only = re.search(r"\bnam\s+(202\d)\b", ascii_text)
            if m_yr_only:
                yy = int(m_yr_only.group(1))
                end_str = (
                    as_of.isoformat() if yy == as_of.year else f"{yy:04d}-12-31"
                )
                return TimeContext(
                    mode="period",
                    primary=Period(f"{yy:04d}-01-01", end_str, f"year_{yy}"),
                    compare=Period(f"{yy-1:04d}-01-01", f"{yy-1:04d}-12-31", f"year_{yy-1}"),
                    shift=shift,
                    compare_shift=cmp_shift,
                    rule_id="R14_year",
                    confidence=1.0,
                    has_time_cue=True,
                    as_of=as_of_date_str,
                )
            if "nam nay" in ascii_text:
                return TimeContext(
                    mode="period",
                    primary=Period(f"{as_of.year:04d}-01-01", as_of.isoformat(), "this_year"),
                    shift=shift,
                    compare_shift=cmp_shift,
                    rule_id="R14_this_year",
                    confidence=1.0,
                    has_time_cue=True,
                    as_of=as_of_date_str,
                )
            if "nam ngoai" in ascii_text or "nam truoc" in ascii_text:
                yy = as_of.year - 1
                return TimeContext(
                    mode="period",
                    primary=Period(f"{yy:04d}-01-01", f"{yy:04d}-12-31", "last_year"),
                    shift=shift,
                    compare_shift=cmp_shift,
                    rule_id="R14_last_year",
                    confidence=1.0,
                    has_time_cue=True,
                    as_of=as_of_date_str,
                )

            # Fallback: không khớp mẫu thời gian nào
            d_str = as_of.isoformat()
            return TimeContext(
                mode="none",
                primary=Period(d_str, d_str, "default_today"),
                shift=shift,
                compare_shift=cmp_shift,
                rule_id="none",
                confidence=0.0,
                has_time_cue=cue,
                as_of=as_of_date_str,
            )

        except Exception:
            return TimeContext(
                mode="none",
                primary=Period(as_of_date_str, as_of_date_str, "error_fallback"),
                rule_id="error",
                confidence=0.0,
                has_time_cue=False,
                as_of=as_of_date_str,
            )

    def _build_this_month(
        self,
        as_of: date,
        as_of_date_str: str,
        shift: Optional[str],
        cmp_shift: Optional[str],
    ) -> TimeContext:
        start_d = f"{as_of.year:04d}-{as_of.month:02d}-01"
        end_d = as_of.isoformat()
        prev_year = as_of.year if as_of.month > 1 else as_of.year - 1
        prev_month = as_of.month - 1 if as_of.month > 1 else 12
        _, prev_month_days = calendar.monthrange(prev_year, prev_month)
        compare_day = min(as_of.day, prev_month_days)
        cmp_start = f"{prev_year:04d}-{prev_month:02d}-01"
        cmp_end = f"{prev_year:04d}-{prev_month:02d}-{compare_day:02d}"
        return TimeContext(
            mode="period",
            primary=Period(start_d, end_d, f"{as_of.year:04d}-{as_of.month:02d}"),
            compare=Period(cmp_start, cmp_end, f"{prev_year:04d}-{prev_month:02d}_mtd"),
            shift=shift,
            compare_shift=cmp_shift,
            rule_id="R12_this_month",
            confidence=1.0,
            has_time_cue=True,
            as_of=as_of_date_str,
        )

    def parse_time_period(self, text: str, as_of_date_str: str) -> Dict[str, Any]:
        """Hàm tương thích ngược 100% với TimeParser.parse_time_period từ code cũ."""
        tctx = self.resolve(text, as_of_date_str)
        return tctx.to_legacy_dict()


# Alias tên class để giữ tương thích hoàn toàn với code cũ
TimeParser = TimeResolver

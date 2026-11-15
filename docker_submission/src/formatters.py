import json
import math
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd


def round_half_up(val: float, ndigits: int = 0) -> Any:
    """
    Làm tròn chuẩn toán học ROUND_HALF_UP (nửa đồng / .5 làm tròn lên theo Rulebook §3 & §9).
    Tránh lỗi banker's rounding (round half to even) của hàm round() mặc định trong Python.
    """
    d = Decimal(str(float(val)))
    if ndigits <= 0:
        q = d.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return int(q)
    fmt = "0." + ("0" * ndigits)
    q = d.quantize(Decimal(fmt), rounding=ROUND_HALF_UP)
    return float(q)


def to_native(val: Any) -> Any:
    """Chuyển đổi mọi kiểu dữ liệu numpy / pandas sang kiểu Python thuần để serialize JSON an toàn."""
    if val is None:
        return None
    if isinstance(val, (bool, np.bool_)):
        return bool(val)
    if isinstance(val, (int, np.integer)):
        return int(val)
    if isinstance(val, (float, np.floating)):
        if math.isnan(float(val)) or math.isinf(float(val)):
            return None
        return float(val)
    if isinstance(val, (pd.Timestamp,)):
        return val.strftime("%Y-%m-%d")
    if isinstance(val, pd.DataFrame):
        cols = [str(c) for c in val.columns]
        rows = [[to_native(x) for x in r] for r in val.to_numpy().tolist()]
        return {"columns": cols, "rows": rows}
    if isinstance(val, pd.Series):
        return [to_native(x) for x in val.tolist()]
    if isinstance(val, (list, tuple, np.ndarray)):
        return [to_native(x) for x in val]
    if isinstance(val, dict):
        return {str(k): to_native(v) for k, v in val.items()}
    return val


def format_number(val: Any, is_currency: bool = True) -> Optional[Any]:
    """
    Rulebook §9:
    - Tiền là số nguyên VND, làm tròn về đồng, nửa đồng làm tròn lên.
    - Số lượng / số đếm: nếu nguyên thì trả int, nếu lẻ thì làm tròn 2 chữ số thập phân.
    """
    val = to_native(val)
    if val is None or isinstance(val, bool):
        return None
    try:
        f_val = float(val)
        if math.isnan(f_val) or math.isinf(f_val):
            return None
    except Exception:
        return None

    if is_currency:
        return int(math.floor(f_val + 0.5))
    else:
        if isinstance(val, int) or abs(f_val - round(f_val)) < 1e-6:
            return int(round(f_val))
        return round_half_up(f_val, 2)


def format_percent(val: Any) -> Optional[float]:
    """
    Rulebook §9:
    - JSON number là số phần trăm: 12,4% ghi 12.4, KHÔNG kèm ký hiệu %, KHÔNG chia 100.
    - Làm tròn 2 chữ số thập phân. Giảm thì ra số âm.
    """
    val = to_native(val)
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, str):
        val = val.replace("%", "").replace(",", ".").strip()
    try:
        f_val = float(val)
        if math.isnan(f_val) or math.isinf(f_val):
            return None
    except Exception:
        return None
    return round_half_up(f_val, 2)


def format_date(val: Any, month_only: bool = False) -> Optional[str]:
    """
    Rulebook §9:
    - Chuỗi ngày ISO "YYYY-MM-DD"; câu hỏi theo tháng: "YYYY-MM".
    """
    if val is None:
        return None
    if isinstance(val, pd.Timestamp):
        s = val.strftime("%Y-%m-%d")
    else:
        s = str(val).strip()
    if s.lower() in ["none", "null", "nan", "nat", ""]:
        return None

    # Chuẩn hóa nếu chuỗi đang ở dạng DD/MM/YYYY
    m_dmy = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", s)
    if m_dmy:
        s = f"{int(m_dmy.group(3)):04d}-{int(m_dmy.group(2)):02d}-{int(m_dmy.group(1)):02d}"
    m_my = re.match(r"^(\d{1,2})[/-](\d{4})$", s)
    if m_my:
        s = f"{int(m_my.group(2)):04d}-{int(m_my.group(1)):02d}"

    if month_only:
        res = s[:7]
        return res if re.match(r"^\d{4}-\d{2}$", res) else None
    res = s[:10]
    if re.match(r"^\d{4}-\d{2}-\d{2}$", res) or re.match(r"^\d{4}-\d{2}$", res):
        return res
    return None


def format_boolean(val: Any) -> Optional[bool]:
    """
    Rulebook §9:
    - boolean: true / false (chữ thường JSON).
    - Vá lỗi code cũ: xử lý đúng chuỗi 'false'/'không'/'0' thay vì ép bool('false') == True.
    """
    val = to_native(val)
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        if val == 1:
            return True
        if val == 0:
            return False
        return None
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("true", "đúng", "dung", "có", "co", "yes"):
            return True
        if s in ("false", "sai", "không", "khong", "no"):
            return False
    return None


def format_string(val: Any) -> Optional[str]:
    """
    Rulebook §9:
    - Tên như trong dữ liệu, đủ dấu tiếng Việt. Không bọc trong mảng.
    """
    val = to_native(val)
    if val is None:
        return None
    if isinstance(val, list):
        if len(val) == 1:
            val = val[0]
        else:
            return None
    if isinstance(val, (dict, bool)):
        return None
    s = str(val).strip()
    if not s or s.lower() in ["none", "null", "nan", "nat"]:
        return None
    return s


def format_list(items: Any) -> Optional[List[Any]]:
    """
    Rulebook §9:
    - Mảng JSON, có thứ tự; một phần tử vẫn là mảng.
    """
    items = to_native(items)
    if items is None:
        return None
    if isinstance(items, dict):
        return None
    if not isinstance(items, list):
        items = [items]
    if len(items) == 0:
        return None

    clean: List[Any] = []
    for it in items:
        it_nat = to_native(it)
        if it_nat is None:
            continue
        if isinstance(it_nat, (int, float)) and not isinstance(it_nat, bool):
            f_it = float(it_nat)
            if math.isnan(f_it) or math.isinf(f_it):
                continue
            clean.append(
                int(round(f_it))
                if abs(f_it - round(f_it)) < 1e-6
                else round_half_up(f_it, 2)
            )
        else:
            s_it = str(it_nat).strip()
            if s_it and s_it.lower() not in ["none", "null", "nan", "nat"]:
                clean.append(s_it)
    return clean if clean else None


def format_table(
    columns: Any, rows: Optional[Any] = None
) -> Optional[Dict[str, Any]]:
    """
    Rulebook §9:
    - {"columns": [...], "rows": [[...], ...]}, ô số là JSON number.
    - Hỗ trợ truyền vào dict {'columns': ..., 'rows': ...} hoặc DataFrame hoặc (columns, rows).
    """
    if isinstance(columns, pd.DataFrame):
        df_nat = to_native(columns)
        columns = df_nat.get("columns", [])
        rows = df_nat.get("rows", [])
    elif isinstance(columns, dict) and rows is None:
        rows = columns.get("rows")
        columns = columns.get("columns")

    columns = to_native(columns)
    rows = to_native(rows)
    if not columns or not rows or not isinstance(rows, list) or len(rows) == 0:
        return None

    clean_cols = [str(c).strip() for c in columns]
    clean_rows: List[List[Any]] = []
    for r in rows:
        if not isinstance(r, (list, tuple)) or len(r) == 0:
            continue
        c_row: List[Any] = []
        for idx, cell in enumerate(r):
            c_nat = to_native(cell)
            if c_nat is None:
                return None
            if idx > 0 and isinstance(c_nat, (int, float)) and not isinstance(c_nat, bool):
                f_c = float(c_nat)
                if math.isnan(f_c) or math.isinf(f_c):
                    return None
                c_row.append(
                    int(math.floor(f_c + 0.5))
                    if abs(f_c - round(f_c)) < 1e-6 or f_c >= 1000
                    else round_half_up(f_c, 2)
                )
            else:
                c_row.append(c_nat)
        clean_rows.append(c_row)

    if not clean_rows:
        return None
    return {"columns": clean_cols, "rows": clean_rows}


def format_unanswerable(reason: str = "") -> Dict[str, Any]:
    clean_reason = (
        str(reason).strip()
        if isinstance(reason, str) and reason.strip()
        else "Dữ liệu phát hành không lưu thông tin này"
    )
    return {"type": "unanswerable", "reason": clean_reason}


def format_abstain(qid: str = "") -> Dict[str, Any]:
    if qid:
        return {"id": qid, "type": "abstain"}
    return {"type": "abstain"}


def make_submission_record(
    qid: str,
    ans_type: str,
    answer: Any = None,
    reason: str = "",
    is_currency: bool = True,
    month_only: bool = False,
) -> Dict[str, Any]:
    """
    Hàm chốt chặn cuối cùng chuẩn hóa bản ghi nộp bài theo đúng Rulebook §9 & DATA MATE.docx.
    Đảm bảo:
    1. 'id' luôn là khóa đầu tiên.
    2. 'type' luôn là khóa thứ hai.
    3. Bất kỳ giá trị None, NaN, rỗng hoặc sai định dạng đều tự động chuyển về {"id": qid, "type": "abstain"}
       để bảo toàn 0 điểm và tuyệt đối không bao giờ bị trừ -1 điểm vì lỗi format.
    """
    ans_type = str(ans_type).strip().lower() if ans_type else "abstain"

    if ans_type == "unanswerable":
        clean_reason = (
            str(reason).strip()
            if isinstance(reason, str) and reason.strip()
            else "Dữ liệu phát hành không đủ để trả lời câu hỏi này"
        )
        return {"id": qid, "type": "unanswerable", "reason": clean_reason}

    if ans_type == "abstain" or answer is None:
        return {"id": qid, "type": "abstain"}

    if ans_type == "number":
        formatted = format_number(answer, is_currency=is_currency)
        if formatted is None:
            return {"id": qid, "type": "abstain"}
        return {"id": qid, "type": "number", "answer": formatted}

    if ans_type == "percent":
        formatted = format_percent(answer)
        if formatted is None:
            return {"id": qid, "type": "abstain"}
        return {"id": qid, "type": "percent", "answer": formatted}

    if ans_type == "string":
        formatted = format_string(answer)
        if formatted is None:
            return {"id": qid, "type": "abstain"}
        return {"id": qid, "type": "string", "answer": formatted}

    if ans_type == "date":
        formatted = format_date(answer, month_only=month_only)
        if formatted is None:
            return {"id": qid, "type": "abstain"}
        return {"id": qid, "type": "date", "answer": formatted}

    if ans_type == "boolean":
        formatted = format_boolean(answer)
        if formatted is None:
            return {"id": qid, "type": "abstain"}
        return {"id": qid, "type": "boolean", "answer": formatted}

    if ans_type == "list":
        formatted = format_list(answer)
        if formatted is None:
            return {"id": qid, "type": "abstain"}
        return {"id": qid, "type": "list", "answer": formatted}

    if ans_type == "table":
        formatted = format_table(answer)
        if formatted is None:
            return {"id": qid, "type": "abstain"}
        return {"id": qid, "type": "table", "answer": formatted}

    return {"id": qid, "type": "abstain"}


def dumps_submission_record(record: Dict[str, Any]) -> str:
    """Serialize bản ghi ra chuỗi JSON 1 dòng, chặn tuyệt đối NaN/Infinity."""
    qid = str(record.get("id", ""))
    try:
        return json.dumps(record, ensure_ascii=False, allow_nan=False)
    except Exception:
        return json.dumps({"id": qid, "type": "abstain"}, ensure_ascii=False)

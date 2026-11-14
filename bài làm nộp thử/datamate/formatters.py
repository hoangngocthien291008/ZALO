import math

def format_number(val, is_currency=True):
    if val is None:
        return None
    try:
        f_val = float(val)
        if math.isnan(f_val):
            return None
    except Exception:
        return None

    if is_currency:
        # Rulebook §48 & §133: Tiền là số nguyên VND, làm tròn về đồng, nửa đồng làm tròn lên
        return int(math.floor(f_val + 0.5))
    else:
        # Rulebook §9: Integer or float
        if isinstance(val, int) or f_val == int(f_val):
            return int(f_val)
        return round(f_val, 2)

def format_percent(val):
    if val is None:
        return None
    try:
        f_val = float(val)
        if math.isnan(f_val):
            return None
    except Exception:
        return None
    # Rulebook §9 & §133: 2 decimal places, JSON number, negative if decreased
    return round(f_val, 2)

def format_date(val, month_only=False):
    if not val:
        return None
    s = str(val).strip()
    if s.lower() in ['none', 'null', 'nan', '']:
        return None
    if month_only:
        return s[:7]
    return s[:10]

def format_boolean(val):
    return bool(val)

def format_string(val):
    if not val:
        return None
    s = str(val).strip()
    if s.lower() in ['none', 'null', 'nan', '']:
        return None
    return s

def format_list(items):
    if items is None:
        return None
    if not isinstance(items, list):
        items = [items]
    if len(items) == 0:
        return None
    # Filter out empty or None
    clean = []
    for it in items:
        if it is not None and str(it).strip().lower() not in ['none', 'null', 'nan', '']:
            clean.append(it)
    return clean if clean else None

def format_table(columns, rows):
    if not rows or len(rows) == 0:
        return None
    return {
        "columns": columns,
        "rows": rows
    }

def format_unanswerable(reason=""):
    return {
        "type": "unanswerable",
        "reason": reason
    }

def format_abstain(qid=""):
    if qid:
        return {
            "id": qid,
            "type": "abstain"
        }
    return {
        "type": "abstain"
    }

def make_submission_record(qid, ans_type, answer, reason=""):
    """
    Constructs a submission JSON record strictly adhering to Rulebook §9 and DATA MATE.docx.
    Guarantees:
    1. 'id' is ALWAYS the first key.
    2. 'type' is ALWAYS the second key.
    3. If answer is None or invalid, falls back safely to {"id": qid, "type": "abstain"} (0 pts penalty).
    """
    if ans_type == 'abstain' or answer is None:
        if ans_type == 'unanswerable':
            return {
                "id": qid,
                "type": "unanswerable",
                "reason": reason if reason else "Dữ liệu không lưu thông tin này"
            }
        return {
            "id": qid,
            "type": "abstain"
        }

    if ans_type == 'unanswerable':
        return {
            "id": qid,
            "type": "unanswerable",
            "reason": reason if reason else "Dữ liệu không lưu thông tin này"
        }

    # Strict validation per type
    if ans_type == 'number':
        try:
            f = float(answer)
            if math.isnan(f):
                return {"id": qid, "type": "abstain"}
            ans = int(f) if f == int(f) else round(f, 2)
            return {
                "id": qid,
                "type": "number",
                "answer": ans
            }
        except Exception:
            return {"id": qid, "type": "abstain"}

    elif ans_type == 'percent':
        try:
            f = float(answer)
            if math.isnan(f):
                return {"id": qid, "type": "abstain"}
            return {
                "id": qid,
                "type": "percent",
                "answer": round(f, 2)
            }
        except Exception:
            return {"id": qid, "type": "abstain"}

    elif ans_type == 'string':
        s = str(answer).strip()
        if not s or s.lower() in ['none', 'null', 'nan']:
            return {"id": qid, "type": "abstain"}
        return {
            "id": qid,
            "type": "string",
            "answer": s
        }

    elif ans_type == 'date':
        s = str(answer).strip()
        if not s or s.lower() in ['none', 'null', 'nan']:
            return {"id": qid, "type": "abstain"}
        return {
            "id": qid,
            "type": "date",
            "answer": s
        }

    elif ans_type == 'boolean':
        return {
            "id": qid,
            "type": "boolean",
            "answer": bool(answer)
        }

    elif ans_type == 'list':
        if not isinstance(answer, list) or len(answer) == 0:
            return {"id": qid, "type": "abstain"}
        return {
            "id": qid,
            "type": "list",
            "answer": answer
        }

    elif ans_type == 'table':
        if not isinstance(answer, dict) or not answer.get('rows'):
            return {"id": qid, "type": "abstain"}
        return {
            "id": qid,
            "type": "table",
            "answer": answer
        }

    return {
        "id": qid,
        "type": "abstain"
    }

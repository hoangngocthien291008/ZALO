import difflib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd

from .preprocessor import NormalizedQuestion, strip_accents

try:
    from rapidfuzz import fuzz as _rf_fuzz
except ImportError:
    _rf_fuzz = None


def normalize_query_str(s: Any) -> str:
    """Kế thừa từ query_executors.py cũ, bổ sung chuẩn hóa khoảng trắng đơn vị đo lường."""
    if not s:
        return ""
    res = re.sub(r"\s+", " ", str(s).lower()).strip()
    res = re.sub(r"(?<=\d)\s*x\s*(?=\d)", "x", res)
    res = re.sub(r"(\d+(?:\.\d+)?)\s+(ml|l|g|kg|mg|mcg|gb|tb|mm|cm|m)\b", r"\1\2", res)
    res = re.sub(r"\b([a-zA-Z])\s+(\d+)\b", r"\1\2", res)
    res = re.sub(r"(\w+)\.\s+(\w+)", r"\1.\2", res)
    return res


def _token_set_ratio(s1: str, s2: str) -> float:
    """
    Tính điểm tương đồng Token Set Ratio (0.0 .. 1.0).
    Sử dụng rapidfuzz nếu đã cài đặt, hoặc thuật toán tương đương chính xác bằng difflib.
    """
    if not s1 or not s2:
        return 0.0
    if _rf_fuzz is not None:
        r_set = _rf_fuzz.token_set_ratio(s1, s2) / 100.0
        r_std = _rf_fuzz.ratio(s1, s2) / 100.0
        return 0.65 * r_set + 0.35 * r_std

    t1 = set(s1.split())
    t2 = set(s2.split())
    if not t1 or not t2:
        return 0.0
    inter = sorted(t1 & t2)
    diff1 = sorted(t1 - t2)
    diff2 = sorted(t2 - t1)

    sect = " ".join(inter)
    combined1 = (sect + " " + " ".join(diff1)).strip()
    combined2 = (sect + " " + " ".join(diff2)).strip()
    full1 = " ".join(sorted(t1))
    full2 = " ".join(sorted(t2))

    scores = [
        difflib.SequenceMatcher(None, sect, combined1).ratio() if sect else 0.0,
        difflib.SequenceMatcher(None, sect, combined2).ratio() if sect else 0.0,
        difflib.SequenceMatcher(None, combined1, combined2).ratio(),
        difflib.SequenceMatcher(None, full1, full2).ratio(),
    ]
    r_set = max(scores)
    r_std = difflib.SequenceMatcher(None, s1, s2).ratio()
    return 0.65 * r_set + 0.35 * r_std


def match_item_rows(df: pd.DataFrame, item_query: str) -> pd.DataFrame:
    """
    Kế thừa chính xác từ bài làm nộp thử/datamate/query_executors.py:
    Khớp các dòng mặt hàng trong DataFrame theo 4 tầng:
    1. Exact match (có dấu)
    2. Substring match (có dấu)
    3. Token match (tất cả từ khóa có độ dài > 1)
    4. Accent-insensitive match (không dấu)
    5. Fuzzy fallback (>= 0.70)
    """
    if df is None or df.empty or not item_query:
        return pd.DataFrame()
    q_l = normalize_query_str(item_query)
    if not q_l:
        return pd.DataFrame()

    norm_names = df["item_name"].apply(normalize_query_str)

    # 0. Exact match không dấu (gom cả biến thể có dấu và không dấu của cùng mặt hàng)
    _q_na0 = strip_accents(q_l)
    _names_na0 = norm_names.apply(strip_accents)
    _exact_na0 = df[_names_na0 == _q_na0]
    if not _exact_na0.empty:
        return _exact_na0

    # 1. Exact match
    exact = df[norm_names == q_l]
    if not exact.empty:
        return exact

    # 2. Substring match
    sub = df[norm_names.str.contains(re.escape(q_l), regex=True, na=False)]
    if not sub.empty:
        return sub

    # 3. Token match
    words = [w for w in q_l.split() if len(w) > 1]
    if len(words) >= 2:

        def has_all(s: str) -> bool:
            return all(w in s for w in words)

        tok_match = df[norm_names.apply(has_all)]
        if not tok_match.empty:
            return tok_match

    # 4. Accent-insensitive match
    q_no_acc = strip_accents(q_l)
    names_no_acc = norm_names.apply(strip_accents)
    exact_na = df[names_no_acc == q_no_acc]
    if not exact_na.empty:
        return exact_na
    sub_na = df[names_no_acc.str.contains(re.escape(q_no_acc), regex=True, na=False)]
    if not sub_na.empty:
        return sub_na

    # 5. Fuzzy fallback với ngưỡng >= 0.70
    unique_names = df["item_name"].dropna().unique()
    best_name = None
    best_score = 0.0
    for uname in unique_names:
        un_ascii = strip_accents(normalize_query_str(uname))
        sc = _token_set_ratio(q_no_acc, un_ascii)
        if sc > best_score:
            best_score = sc
            best_name = uname

    if best_name is not None and best_score >= 0.70:
        return df[df["item_name"] == best_name]

    return pd.DataFrame()


@dataclass
class EntityCandidate:
    kind: str  # "item" | "customer" | "group" | "active_ingredient"
    code: str
    name: str
    score: float
    matched_span: str = ""


@dataclass
class EntityResult:
    needs_entity: bool
    candidates: Dict[str, List[EntityCandidate]] = field(default_factory=dict)
    best_score: float = 0.0
    status: str = "not_needed"  # "exact" | "fuzzy" | "not_found" | "not_needed"


class EntityMatcher:
    """
    Module Liên kết Thực thể (Bước 2.2 trong Kiến trúc):
    - Kết hợp Exact/Substring không dấu và RapidFuzz (Token Set Ratio).
    - Nếu Max Similarity Score >= 0.70: Trích xuất TOP 3 ứng viên có điểm cao nhất để nạp vào prompt cho Qwen chọn.
    - Nếu Max Similarity Score < 0.70 (khi câu hỏi có yêu cầu tìm mặt hàng/khách cụ thể):
      kích hoạt cảnh báo `not_found` để đánh giá Unanswerable hoặc Abstain an toàn.
    """

    def __init__(self, threshold: float = 0.70, top_k: int = 3) -> None:
        self.threshold = threshold
        self.top_k = top_k

    def build_index(self, views: Dict[str, pd.DataFrame]) -> Dict[str, List[Tuple[str, str, str]]]:
        """
        Xây dựng chỉ mục thực thể cho 1 cửa hàng:
        Mỗi mục gồm danh sách (code, name, ascii_norm_name).
        """
        index: Dict[str, List[Tuple[str, str, str]]] = {
            "item": [],
            "customer": [],
            "group": [],
            "active_ingredient": [],
            "staff": [],
            "supplier": [],
        }

        seen_items: Set[str] = set()
        for vname in ["products", "sales", "imports", "inventory"]:
            vdf = views.get(vname)
            if vdf is not None and not vdf.empty and "item_name" in vdf.columns:
                codes = (
                    vdf["item_code"].fillna("").astype(str)
                    if "item_code" in vdf.columns
                    else vdf["item_name"].fillna("").astype(str)
                )
                names = vdf["item_name"].fillna("").astype(str)
                for c, n in zip(codes, names):
                    n_s = n.strip()
                    c_s = c.strip() or n_s
                    if (
                        not n_s
                        or n_s.lower() in ("nan", "none", "đơn hàng", "phí phục vụ")
                        or n_s.lower() in seen_items
                    ):
                        continue
                    seen_items.add(n_s.lower())
                    index["item"].append(
                        (c_s, n_s, strip_accents(normalize_query_str(n_s)))
                    )

        seen_custs: Set[str] = set()
        for vname in ["customers", "sales", "orders"]:
            vdf = views.get(vname)
            if vdf is not None and not vdf.empty and "cust_name" in vdf.columns:
                cids = (
                    vdf["cust_id"].fillna("").astype(str)
                    if "cust_id" in vdf.columns
                    else vdf["cust_name"].fillna("").astype(str)
                )
                cnames = vdf["cust_name"].fillna("").astype(str)
                for cid, cname in zip(cids, cnames):
                    cn_s = cname.strip()
                    if (
                        not cn_s
                        or cn_s.lower()
                        in ("nan", "none", "khách lẻ", "khach le", "khách vãng lai")
                        or cn_s.lower() in seen_custs
                    ):
                        continue
                    seen_custs.add(cn_s.lower())
                    index["customer"].append(
                        (
                            cid.strip() or cn_s,
                            cn_s,
                            strip_accents(normalize_query_str(cn_s)),
                        )
                    )

        seen_groups: Set[str] = set()
        for vname in ["products", "sales"]:
            vdf = views.get(vname)
            if vdf is not None and not vdf.empty and "item_group" in vdf.columns:
                for grp in vdf["item_group"].dropna().unique():
                    g_s = str(grp).strip()
                    if g_s and g_s.lower() not in ("nan", "none") and g_s.lower() not in seen_groups:
                        seen_groups.add(g_s.lower())
                        index["group"].append(
                            (g_s, g_s, strip_accents(normalize_query_str(g_s)))
                        )

        p_df = views.get("products")
        if p_df is not None and not p_df.empty and "active_ingredient" in p_df.columns:
            seen_act: Set[str] = set()
            for act in p_df["active_ingredient"].dropna().unique():
                a_s = str(act).strip()
                if a_s and a_s.lower() not in ("nan", "none") and a_s.lower() not in seen_act:
                    seen_act.add(a_s.lower())
                    index["active_ingredient"].append(
                        (a_s, a_s, strip_accents(normalize_query_str(a_s)))
                    )

        seen_staff: Set[str] = set()
        for vname in ["sales", "orders"]:
            vdf = views.get(vname)
            if vdf is not None and not vdf.empty and "staff" in vdf.columns:
                for st in vdf["staff"].dropna().unique():
                    st_s = str(st).strip()
                    if st_s and st_s.lower() not in ("nan", "none") and st_s.lower() not in seen_staff:
                        seen_staff.add(st_s.lower())
                        index["staff"].append(
                            (st_s, st_s, strip_accents(normalize_query_str(st_s)))
                        )

        imp_df = views.get("imports")
        if imp_df is not None and not imp_df.empty and "supplier" in imp_df.columns:
            seen_sup: Set[str] = set()
            for sup in imp_df["supplier"].dropna().unique():
                sup_s = str(sup).strip()
                if sup_s and sup_s.lower() not in ("nan", "none") and sup_s.lower() not in seen_sup:
                    seen_sup.add(sup_s.lower())
                    index["supplier"].append(
                        (sup_s, sup_s, strip_accents(normalize_query_str(sup_s)))
                    )

        return index

    def extract_shop_vocab(
        self, index: Dict[str, List[Tuple[str, str, str]]]
    ) -> Set[str]:
        vocab: Set[str] = set()
        for entries in index.values():
            for code, name, norm_ascii in entries:
                vocab.add(code.lower())
                for w in re.findall(r"[^\W\d_]+", name.lower()):
                    vocab.add(w)
                for w in re.findall(r"[^\W\d_]+", norm_ascii):
                    vocab.add(w)
        return vocab

    def match(
        self,
        nq: NormalizedQuestion,
        index: Dict[str, List[Tuple[str, str, str]]],
    ) -> EntityResult:
        """
        Tìm kiếm thực thể trong câu hỏi đã chuẩn hóa.
        Trả về TOP 3 ứng viên có điểm >= threshold (0.70).
        """
        q_ascii = nq.ascii
        # Chuẩn hóa khoảng trắng số đo, kích thước, mã hàng và dấu chấm
        q_ascii = re.sub(r"(?<=\d)\s*x\s*(?=\d)", "x", q_ascii)
        q_ascii = re.sub(
            r"(\d+(?:\.\d+)?)\s+(ml|l|g|kg|mg|mcg|gb|tb|mm|cm|m)\b", r"\1\2", q_ascii
        )
        q_ascii = re.sub(r"\b([a-zA-Z])\s+(\d+)\b", r"\1\2", q_ascii)
        q_ascii = re.sub(r"(\w+)\.\s+(\w+)", r"\1.\2", q_ascii)
        candidates: Dict[str, List[EntityCandidate]] = {}
        best_overall = 0.0

        # Kiểm tra xem câu hỏi có nhắc đến một mặt hàng / hoạt chất cụ thể không
        needs_item = bool(
            re.search(
                r"\b(mat hang|san pham|mon|thuoc|hoat chat)\s+(?!nao|gi|chay|ban|e\b|khong\b|hot|loi|lai|ua|kem|it|nhieu|chi\b|ke don|trong|thang|nam|ngay|quy|tuan|hom|dot|cua\b|co\b)",
                q_ascii,
            )
            or re.search(
                r"ban\s+(?:ra|duoc)?\s*(?:tong cong\s*)?(?:bao nhieu|may)\s+(?!don|hoa don|bill|phieu|tien|dong|ngay|thang|khach|lan)[a-z]+\s+[a-z0-9]{2,}",
                q_ascii,
            )
            or re.search(
                r"\b(?:so|bao nhieu|may)\s+(?:lo|chai|lon|hop|vi|goi|vien|tuyp|bich|thung|ong|chiec|cai|ly|coc|suat|dia|to|phan|kg|lit)\s+(?!thuoc\b|hang\b|nao\b|gi\b|trong\b|ban\b)[a-z0-9]{2,}",
                q_ascii,
            )
        )

        for kind, entries in index.items():
            if not entries:
                continue
            scored: List[EntityCandidate] = []

            # Tầng 1: Substring khớp chính xác trên ASCII (lọc nhanh `norm_a in q_ascii` trước khi kiểm tra ranh giới từ)
            exact_hits: List[EntityCandidate] = []
            for code, name, norm_a in entries:
                if len(norm_a) >= 2 and norm_a in q_ascii:
                    if re.search(
                        r"(?<!\w)" + re.escape(norm_a) + r"(?!\w)", q_ascii
                    ):
                        exact_hits.append(
                            EntityCandidate(
                                kind=kind,
                                code=code,
                                name=name,
                                score=1.0,
                                matched_span=norm_a,
                            )
                        )

            # Tầng 1.5: Khớp phần tên chính của mặt hàng khi đã lược bỏ quy cách đóng gói (VD: 'Efticol 0,9% lốc 10 lọ x 10ml' -> 'efticol 0,9%')
            if not exact_hits and kind == "item":
                core_matches: List[Tuple[str, str, str]] = []
                for code, name, norm_a in entries:
                    core_a = re.sub(
                        r"\s+(?:loc|hop|vi|chai|lon|goi|tuyp|lo|bich|thung|ong)\s+\d+.*$",
                        "",
                        norm_a,
                    ).strip()
                    # Không chấp nhận core_a nếu chỉ còn 1 từ chung chung (như 'thep', 'gach', 'son'...) hoặc là tên hàng đặc thù
                    words = core_a.split()
                    if (
                        len(core_a) >= 4
                        and len(words) >= 2
                        and core_a not in {"thep hop", "gach ong", "ong nuoc"}
                        and core_a != norm_a
                        and core_a in q_ascii
                        and re.search(
                            r"(?<!\w)" + re.escape(core_a) + r"(?!\w)", q_ascii
                        )
                    ):
                        core_matches.append((code, name, core_a))
                if core_matches:
                    sc_val = 0.96 if len(core_matches) == 1 else 0.88
                    for code, name, core_a in core_matches:
                        exact_hits.append(
                            EntityCandidate(
                                kind=kind,
                                code=code,
                                name=name,
                                score=sc_val,
                                matched_span=core_a,
                            )
                        )

            if exact_hits:
                # Ưu tiên thực thể có tên dài hơn (ví dụ 'Bia Tiger Bạc lon' ưu tiên hơn 'Bia Tiger lon')
                exact_hits.sort(key=lambda c: (-len(c.matched_span), c.code))
                filtered_exact: List[EntityCandidate] = []
                for cand in exact_hits:
                    if not any(
                        cand.matched_span != other.matched_span
                        and cand.matched_span in other.matched_span
                        for other in filtered_exact
                    ):
                        filtered_exact.append(cand)
                scored = filtered_exact[: self.top_k]
            elif needs_item and kind in ("item", "active_ingredient"):
                # Tầng 2: Fuzzy N-gram matching với RapidFuzz / Token Set Ratio khi câu hỏi cần tìm mặt hàng cụ thể
                q_words = [
                    w
                    for w in q_ascii.split()
                    if len(w) >= 2
                    and w
                    not in {
                        "cho",
                        "hoi",
                        "xem",
                        "giup",
                        "tinh",
                        "trong",
                        "thang",
                        "tuan",
                        "ngay",
                        "nam",
                        "hom",
                        "nay",
                        "qua",
                        "truoc",
                        "roi",
                        "doanh",
                        "thu",
                        "tien",
                        "ban",
                        "duoc",
                        "bao",
                        "nhieu",
                        "la",
                        "cua",
                        "hang",
                        "quan",
                        "tiem",
                        "tong",
                        "cong",
                        "mat",
                        "san",
                        "pham",
                        "mon",
                        "thuoc",
                    }
                ]
                q_word_set = set(q_words)
                q_clean = " ".join(q_words)
                if q_clean:
                    for code, name, norm_a in entries:
                        if len(norm_a) < 3:
                            continue
                        # Chỉ tính điểm tương đồng nếu có ít nhất 1 token chung hoặc prefix 3 ký tự chung
                        norm_tokens = norm_a.split()
                        if not (q_word_set & set(norm_tokens)) and not any(
                            qw[:3] == nw[:3]
                            for qw in q_words
                            if len(qw) >= 3
                            for nw in norm_tokens
                            if len(nw) >= 3
                        ):
                            continue
                        sc = _token_set_ratio(q_clean, norm_a)
                        if sc >= self.threshold:
                            scored.append(
                                EntityCandidate(
                                    kind=kind,
                                    code=code,
                                    name=name,
                                    score=round(sc, 4),
                                    matched_span=q_clean,
                                )
                            )
                    scored.sort(key=lambda c: (-c.score, c.code))
                    scored = scored[: self.top_k]

            if scored:
                candidates[kind] = scored
                if scored[0].score > best_overall:
                    best_overall = scored[0].score

        if best_overall >= 0.95:
            status = "exact"
        elif best_overall >= self.threshold:
            status = "fuzzy"
        elif needs_item:
            status = "not_found"
        else:
            status = "not_needed"

        return EntityResult(
            needs_entity=needs_item or (best_overall >= self.threshold),
            candidates=candidates,
            best_score=best_overall,
            status=status,
        )

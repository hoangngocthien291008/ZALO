import re
import unicodedata
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple


def nfc(s: str) -> str:
    """Chuẩn hóa chuỗi Unicode về chuẩn NFC."""
    if not s:
        return ""
    return unicodedata.normalize("NFC", str(s))


def strip_accents(s: str) -> str:
    """
    Bỏ dấu tiếng Việt và chuyển 'đ'/'Đ' thành 'd'/'D'.
    Kế thừa từ query_executors.py cũ.
    """
    if not s:
        return ""
    s_str = str(s).replace("đ", "d").replace("Đ", "D")
    return "".join(
        c for c in unicodedata.normalize("NFD", s_str) if unicodedata.category(c) != "Mn"
    )


@dataclass
class NormalizedQuestion:
    raw: str
    text: str
    ascii: str
    tokens: List[str] = field(default_factory=list)
    fixes: List[Tuple[str, str]] = field(default_factory=list)


class Preprocessor:
    """
    Module Tiền xử lý văn bản (Bước 1 trong Kiến trúc 5 bước):
    - Chuẩn hóa Unicode NFC, khoảng trắng.
    - Sửa lỗi gõ Telex tiếng Việt (cuwar -> cửa, tieenf -> tiền, dduowcj -> được, ...),
      bảo vệ tuyệt đối tên thương hiệu và thực thể cửa hàng (voucher, costar, dulux, ...).
    - Mở rộng từ viết tắt và tiếng lóng (ko/k -> không, sp -> sản phẩm, dc -> được, dt/dthu -> doanh thu, nv -> nhân viên, ...).
    - Sinh chuỗi ASCII không dấu phục vụ TimeResolver và EntityMatcher.
    """

    # Bảng từ điển lỗi gõ Telex phổ biến thực tế trong tập câu hỏi DataMate
    DEFAULT_TELEX_MAP = {
        "cuwar": "cửa",
        "cuar": "của",
        "tieenf": "tiền",
        "ddow": "đơ",
        "ddown": "đơn",
        "ddonw": "đơn",
        "dduowcj": "được",
        "duowcj": "được",
        "dduoc": "được",
        "ngayf": "ngày",
        "nhaats": "nhất",
        "nhaat": "nhất",
        "nhaf": "nhà",
        "bans": "bán",
        "laf": "là",
        "laff": "là",
        "ddieenj": "điện",
        "thangs": "tháng",
        "heets": "hết",
        "heet": "hết",
        "ees": "ế",
        "giumf": "giùm",
        "giups": "giúp",
        "huyr": "huỷ",
        "khachs": "khách",
        "soos": "số",
        "vaayj": "vậy",
        "naof": "nào",
        "khoong": "không",
        "rooif": "rồi",
        "laix": "lãi",
        "nhieeu": "nhiều",
        "nhieeuf": "nhiều",
        "trawm": "trăm",
        "chayj": "chạy",
        "tawng": "tăng",
        "giamr": "giảm",
        "hangf": "hàng",
        "hoom": "hôm",
        "laij": "lại",
        "uowc": "ước",
        "nee": "nè",
        "maays": "mấy",
        "kieemr": "kiểm",
        "toongr": "tổng",
        "voiw": "với",
        "voif": "với",
        "tuafn": "tuần",
        "tuaanf": "tuần",
        "nafm": "năm",
        "nawm": "năm",
    }

    # Danh sách các từ tiếng Anh / tên thương hiệu có hình thức giống Telex phải được bảo vệ
    DEFAULT_PROTECTED_VOCAB = {
        "voucher",
        "acyclovir",
        "costar",
        "dulux",
        "powerflexx",
        "power",
        "moony",
        "clear",
        "for",
        "hippis",
        "max",
        "huggies",
        "gas",
        "petrolimex",
        "air",
        "order",
        "total",
        "bill",
        "cod",
        "pos",
        "vat",
        "size",
        "top",
        "hot",
        "full",
        "mini",
        "pro",
        "plus",
        "extra",
        "aquafina",
        "lavie",
        "pepsi",
        "coca",
        "sting",
        "tiger",
        "heineken",
        "vinamilk",
        "yakult",
        "omachi",
        "vifon",
        "jasmine",
        "knorr",
        "maggi",
        "simply",
        "chinsu",
        "vissan",
        "oishi",
        "oreo",
        "chocopie",
        "cosy",
        "alpenliebe",
        "doublemint",
        "omo",
        "ariel",
        "sunlight",
        "colgate",
        "lifebuoy",
        "pulppy",
        "tempo",
        "diana",
        "bobby",
        "vinataba",
        "panasonic",
        "insee",
        "pomina",
        "tuynel",
        "panadol",
        "hapacol",
        "acemuc",
        "efferalgan",
        "berberin",
        "smecta",
        "oresol",
        "augmentin",
        "cefixim",
        "amoxicillin",
        "paracetamol",
        "ibuprofen",
        "omeprazol",
        "loratadin",
        "cetirizin",
        "alpha",
        "choay",
        "decold",
        "tiffy",
        "strepsils",
        "decolgen",
        "fugacar",
        "enterogermina",
        "phosphalugel",
        "gaviscon",
        "castrol",
        "motul",
        "shell",
        "yamaha",
        "honda",
        "suzuki",
        "sym",
        "piaggio",
        "vision",
        "lead",
        "vario",
        "exciter",
        "winner",
        "wave",
        "sirius",
        "future",
        "grande",
        "janus",
        "sh",
        "ab",
        "airblade",
        "michelin",
        "chengshin",
        "casumina",
        "inoue",
        "ngk",
        "denso",
        "gs",
        "globe",
        "iphone",
        "samsung",
        "oppo",
        "xiaomi",
        "vivo",
        "realme",
        "nokia",
        "apple",
        "galaxy",
        "redmi",
        "reno",
        "type",
        "anker",
        "baseus",
        "pisen",
        "remuch",
        "enfa",
        "similac",
        "aptamil",
        "nan",
        "friso",
        "meiji",
        "morinaga",
        "pediasure",
        "ensure",
        "grow",
        "dielac",
        "optimum",
        "merries",
        "pampers",
        "goon",
        "mamamy",
        "pigeon",
        "avent",
        "chicco",
        "wesser",
        "cetaphil",
        "mustela",
        "lactacyd",
    }

    # Bảng mở rộng từ viết tắt đơn (token-level)
    ABBREV_TOKEN_MAP = {
        "mik": "mình",
        "mk": "mình",
        "bnhiu": "bao nhiêu",
        "bnhieu": "bao nhiêu",
        "bnh": "bao nhiêu",
        "bn": "bao nhiêu",
        "nhìu": "nhiều",
        "nhiu": "nhiều",
        "jum": "giúp",
        "gium": "giúp",
        "giup": "giúp",
        "dthu": "doanh thu",
        "dt": "doanh thu",
        "revenue": "doanh thu",
        "hnay": "hôm nay",
        "hqua": "hôm qua",
        "trc": "trước",
        "sp": "sản phẩm",
        "nv": "nhân viên",
        "ncc": "nhà cung cấp",
        "hđ": "hoá đơn",
        "đh": "đơn hàng",
        "sl": "số lượng",
        "slg": "số lượng",
        "đvt": "đơn vị tính",
        "dvt": "đơn vị tính",
        "dc": "được",
        "đc": "được",
        "đươc": "được",
        "ko": "không",
        "hông": "không",
        "hok": "không",
        "vs": "so với",
        "zậy": "vậy",
        "zay": "vậy",
    }

    def __init__(
        self,
        protected_vocab: Optional[Set[str]] = None,
        custom_telex_map: Optional[dict] = None,
    ) -> None:
        self.protected_vocab: Set[str] = set(self.DEFAULT_PROTECTED_VOCAB)
        if protected_vocab:
            self.protected_vocab.update(w.lower().strip() for w in protected_vocab)
        self.shop_vocab: Set[str] = set()
        self.telex_map = dict(self.DEFAULT_TELEX_MAP)
        if custom_telex_map:
            self.telex_map.update(custom_telex_map)

    def set_shop_vocab(self, vocab: Set[str]) -> None:
        """
        Cập nhật từ vựng riêng của cửa hàng (tên hàng hóa, mã hàng, tên khách hàng).
        Các token nằm trong shop_vocab sẽ được bảo vệ, không bị sửa Telex.
        """
        self.shop_vocab = {w.lower().strip() for w in vocab if w and len(w.strip()) >= 2}

    def telex_decode_token(self, tok: str) -> Optional[str]:
        """
        Giải mã một token bị gõ lỗi kiểu Telex.
        Ưu tiên tra bảng DEFAULT_TELEX_MAP đã kiểm chứng trên dữ liệu thật.
        """
        tl = tok.lower()
        if tl in self.protected_vocab or tl in self.shop_vocab:
            return None
        if tl in self.telex_map:
            return self.telex_map[tl]
        return None

    def normalize(self, question: str) -> NormalizedQuestion:
        """
        Thực hiện chuẩn hóa toàn diện câu hỏi đầu vào.
        Đảm bảo an toàn 100% không ném exception.
        """
        raw = str(question) if question is not None else ""
        s = nfc(raw).strip()
        # Chuẩn hóa khoảng trắng và ký tự đặc biệt
        s = re.sub(r"[\u00a0\t\r\n]+", " ", s)
        s = re.sub(r"\s+", " ", s).strip()

        s_lower = s.lower()
        fixes: List[Tuple[str, str]] = []

        # Bước 1: Sửa các cụm từ viết tắt nhiều chữ phổ biến (Kế thừa từ question_parser.py & time_parser.py)
        multi_word_rules = [
            (r"\b(hom ni|hôm ni|bữa nay|bua nay|hom nay)\b", "hôm nay"),
            (r"\b(bữa qua|bua qua|hom qua)\b", "hôm qua"),
            (r"\b(bao nhiu|bao nhieu)\b", "bao nhiêu"),
            (r"\b(d thu)\b", "doanh thu"),
            (r"\b(tháng nay|thang nay)\b", "tháng này"),
            (r"\b(tuần nay|tuan nay)\b", "tuần này"),
            (r"\b(tuần trước|tuan truoc)\b", "tuần trước"),
            (r"\b(tuần rồi|tuan roi)\b", "tuần rồi"),
            (r"\b(tháng trước|thang truoc)\b", "tháng trước"),
            (r"\b(tháng rồi|thang roi)\b", "tháng rồi"),
            (r"\b(năm ngoái|nam ngoai)\b", "năm ngoái"),
            (r"\b(năm trước|nam truoc)\b", "năm trước"),
            (r"\b(năm nay|nam nay)\b", "năm nay"),
            (r"\b(cùng kỳ|cung ky)\b", "cùng kỳ"),
            (r"\b(liền trước|lien truoc|liền trc|lien trc)\b", "liền trước"),
            (r"\b(tu dau|từ dau)\b", "từ đầu"),
            (r"\b(den het|đến het|den hết)\b", "đến hết"),
            (r"\b(tính đến|tinh den)\b", "tính đến"),
            (r"\b(cuối ngày|cuoi ngay)\b", "cuối ngày"),
            (r"\b(ca sang)\b", "ca sáng"),
            (r"\b(ca chieu)\b", "ca chiều"),
            (r"\b(mat hang)\b", "mặt hàng"),
            (r"\b(san pham)\b", "sản phẩm"),
            (r"\b(khach hang)\b", "khách hàng"),
            (r"\b(nhan vien)\b", "nhân viên"),
            (r"\b(ban chay)\b", "bán chạy"),
            (r"\b(chay nhat)\b", "chạy nhất"),
            (r"\b(nhieu nhat)\b", "nhiều nhất"),
            (r"\b(cao nhat)\b", "cao nhất"),
            (r"\b(lon nhat)\b", "lớn nhất"),
            (r"\b(thap nhat)\b", "thấp nhất"),
            (r"\b(it nhat)\b", "ít nhất"),
            (r"\b(tro len)\b", "trở lên"),
            (r"\b(lien tuc)\b", "liên tục"),
            (r"\b(lien nhau)\b", "liền nhau"),
            (r"\b(ket thuc)\b", "kết thúc"),
            (r"\b(binh quan)\b", "bình quân"),
            (r"\b(trung binh)\b", "trung bình"),
            (r"\b(phan tram)\b", "phần trăm"),
            (r"\b(ti le|ty le|tỷ lệ)\b", "tỉ lệ"),
            (r"\b(ti trong|ty trong|tỷ trọng)\b", "tỉ trọng"),
            (r"\b(ke don)\b", "kê đơn"),
            (r"\b(hoat chat)\b", "hoạt chất"),
            (r"\b(ton kho)\b", "tồn kho"),
            (r"\b(con no)\b", "còn nợ"),
            (r"\b(cong no)\b", "công nợ"),
            (r"\b(mua chiu)\b", "mua chịu"),
            (r"\b(nhap hang)\b", "nhập hàng"),
            (r"\b(phieu nhap)\b", "phiếu nhập"),
            (r"\b(tra hang)\b", "trả hàng"),
            (r"\b(bao hanh)\b", "bảo hành"),
            (r"\b(tra gop)\b", "trả góp"),
            (r"\b(lai gop)\b", "lãi gộp"),
            (r"\b(lai rong)\b", "lãi ròng"),
            (r"\b(gia von)\b", "giá vốn"),
        ]

        for pat, repl in multi_word_rules:
            new_s = re.sub(pat, repl, s_lower)
            if new_s != s_lower:
                fixes.append((pat, repl))
                s_lower = new_s

        # Chuẩn hóa th<M>/<Y> và t<M>/<Y> -> tháng <M>/<Y>
        s_lower = re.sub(r"\bth(\d{1,2})[/-](\d{2,4})\b", r"tháng \1/\2", s_lower)
        s_lower = re.sub(r"\bt(\d{1,2})[/-](\d{2,4})\b", r"tháng \1/\2", s_lower)
        s_lower = re.sub(r"\b(\d+)\s+mon\b", r"\1 món", s_lower)
        s_lower = re.sub(r"≥", "từ ", s_lower)

        # Bước 2: Tách từ và xử lý Telex + viết tắt cấp độ token
        raw_tokens = re.findall(r"\d+(?:[.,/:-]\d+)*|[^\W\d_]+|[^\s\w]", s_lower)
        out_tokens: List[str] = []
        n_toks = len(raw_tokens)

        for i, tok in enumerate(raw_tokens):
            # Giữ nguyên dấu câu hoặc số
            if not re.match(r"^[^\W\d_]+$", tok):
                out_tokens.append(tok)
                continue

            # Kiểm tra Telex trước
            telex_fixed = self.telex_decode_token(tok)
            if telex_fixed:
                fixes.append((tok, telex_fixed))
                out_tokens.append(telex_fixed)
                continue

            # Kiểm tra từ viết tắt ngữ cảnh đặc biệt
            if tok == "k":
                # 'k' đứng sau số là nghìn (50k), đứng trước/sau động từ là 'không'
                prev_tok = raw_tokens[i - 1] if i > 0 else ""
                if not re.match(r"^\d+", prev_tok) and tok not in self.shop_vocab:
                    fixes.append((tok, "không"))
                    out_tokens.append("không")
                    continue

            if tok in ("z", "v", "j") and i >= n_toks - 2:
                repl = "gì" if tok == "j" else "vậy"
                fixes.append((tok, repl))
                out_tokens.append(repl)
                continue

            if tok == "th" and i + 1 < n_toks and (
                re.match(r"^\d+$", raw_tokens[i + 1])
                or raw_tokens[i + 1] in ("trước", "này", "rồi", "sau")
            ):
                fixes.append((tok, "tháng"))
                out_tokens.append("tháng")
                continue

            if tok == "total" and i + 1 < n_toks and raw_tokens[i + 1] in ("bao", "bnhiu", "nhiêu", "mấy"):
                fixes.append((tok, "tổng cộng"))
                out_tokens.append("tổng cộng")
                continue

            if tok in self.ABBREV_TOKEN_MAP and tok not in self.shop_vocab:
                repl = self.ABBREV_TOKEN_MAP[tok]
                fixes.append((tok, repl))
                out_tokens.append(repl)
                continue

            out_tokens.append(tok)

        # Ghép lại chuỗi chuẩn hóa
        rebuilt = ""
        for idx, tk in enumerate(out_tokens):
            if idx == 0:
                rebuilt = tk
            elif tk in (",", ".", "?", "!", ":", ";", ")", "%"):
                rebuilt += tk
            elif out_tokens[idx - 1] in ("(",):
                rebuilt += tk
            else:
                rebuilt += " " + tk

        rebuilt = re.sub(r"\s+", " ", rebuilt).strip()
        ascii_str = strip_accents(rebuilt).lower()
        ascii_str = re.sub(r"\s+", " ", ascii_str).strip()

        return NormalizedQuestion(
            raw=raw,
            text=rebuilt,
            ascii=ascii_str,
            tokens=out_tokens,
            fixes=fixes,
        )

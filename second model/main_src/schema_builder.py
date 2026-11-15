import os
import re
import sqlite3
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)

try:
    pd.options.future.infer_string = False
except Exception:
    pass


def clean_currency_str(val: Any) -> float:
    """
    Làm sạch số tiền / số lượng trong bảng tính Việt Nam:
    - Hậu tố 'k' (x1,000), 'tr'/'triệu' (x1,000,000).
    - Ký hiệu 'đ', 'vnd', từ đơn vị kèm sau số ('1 hộp', '5 gói').
    - Dấu chấm phân cách hàng nghìn và dấu phẩy thập phân kiểu Việt Nam.
    Kế thừa từ data_loader.py cũ.
    """
    if pd.isna(val):
        return 0.0
    if isinstance(val, (int, float, np.number)):
        if np.isnan(val):
            return 0.0
        return float(val)
    s = str(val).strip().lower()
    if not s or s in ("nan", "none", "null", "-"):
        return 0.0

    s = s.replace("đ", "").replace("vnd", "").strip()

    # Hỗ trợ dạng viết tắt tiền Việt kẹp giữa: '1tr590' (=1.590.000), '1tr50.000' (=1.050.000)
    m_mid = re.match(r"^([-+]?\d+)\s*(?:tr|triệu)\s*(\d+(?:[.,]\d+)*)$", s)
    if m_mid:
        try:
            g1 = float(m_mid.group(1))
            g2 = m_mid.group(2)
            sign = -1.0 if g1 < 0 else 1.0
            if "." in g2 or "," in g2:
                return g1 * 1000000.0 + sign * float(
                    g2.replace(".", "").replace(",", "")
                )
            return float(f"{int(abs(g1))}.{g2}") * 1000000.0 * sign
        except Exception:
            pass

    multiplier = 1.0
    if re.search(r"\d\s*k$", s):
        multiplier = 1000.0
        s = re.sub(r"k$", "", s).strip()
    elif re.search(r"\d\s*(tr|triệu)$", s):
        multiplier = 1000000.0
        s = re.sub(r"(tr|triệu)$", "", s).strip()

    m_num_unit = re.match(r"^([-+]?\d+(?:[.,]\d+)*)\s+[^\d]+$", s)
    if m_num_unit:
        s = m_num_unit.group(1).strip()

    if multiplier > 1.0:
        # Khi có hậu tố 'k' hoặc 'tr'/'triệu' (vd: '3,035tr', '2.5tr'), dấu phẩy/chấm là phần thập phân
        s = s.replace(",", ".")
    elif "." in s and "," in s:
        if s.rfind(".") > s.rfind(","):
            s = s.replace(",", "")
        else:
            s = s.replace(".", "").replace(",", ".")
    elif "." in s:
        parts = s.split(".")
        if len(parts) > 1 and all(len(p) == 3 for p in parts[1:]):
            s = s.replace(".", "")
        elif len(parts) == 2 and len(parts[1]) == 3:
            s = s.replace(".", "")
    elif "," in s:
        parts = s.split(",")
        if len(parts) > 1 and all(len(p) == 3 for p in parts[1:]):
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")

    try:
        return float(s) * multiplier
    except Exception:
        return 0.0


def parse_date_str(val: Any, default_year: Optional[int] = None) -> Optional[str]:
    """
    Chuẩn hóa mọi định dạng ngày tháng trong Excel/CSV/SQLite về chuỗi ISO 'YYYY-MM-DD'.
    Hỗ trợ: Timestamp, Excel serial date (30000..60000), '25 thg 5 2026', 'dd/mm/yyyy', 'dd/mm' (kèm năm sheet).
    Kế thừa từ data_loader.py cũ.
    """
    if pd.isna(val):
        return None
    if isinstance(val, (datetime, pd.Timestamp)):
        return val.strftime("%Y-%m-%d")
    if isinstance(val, (int, float, np.number)):
        try:
            f_v = float(val)
            if 30000 <= f_v <= 60000:
                dt = pd.to_datetime(f_v, unit="D", origin="1899-12-30")
                return dt.strftime("%Y-%m-%d")
        except Exception:
            pass
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return None
    try:
        f_val = float(s)
        if 30000 <= f_val <= 60000:
            dt = pd.to_datetime(f_val, unit="D", origin="1899-12-30")
            return dt.strftime("%Y-%m-%d")
    except Exception:
        pass

    m_thg = re.search(
        r"(\d{1,2})\s+(?:thg|tháng)\s+(\d{1,2})(?:[,\s]+(\d{4}))?",
        s,
        re.IGNORECASE,
    )
    if m_thg:
        d = int(m_thg.group(1))
        m = int(m_thg.group(2))
        y = int(m_thg.group(3)) if m_thg.group(3) else default_year
        if y and 1 <= m <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{m:02d}-{d:02d}"

    s_date = s.split()[0].split("T")[0]
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            dt = datetime.strptime(s_date, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    m_dm = re.match(r"^(\d{1,2})[/-](\d{1,2})$", s_date)
    if m_dm and default_year:
        d = int(m_dm.group(1))
        m = int(m_dm.group(2))
        if 1 <= m <= 12 and 1 <= d <= 31:
            return f"{default_year:04d}-{m:02d}-{d:02d}"

    return None


def clean_currency_series(s: pd.Series) -> pd.Series:
    """Chuẩn hóa nhanh toàn bộ cột số tiền / số lượng bằng ánh xạ giá trị duy nhất."""
    if s is None or s.empty:
        return pd.Series(dtype=float)
    if pd.api.types.is_numeric_dtype(s):
        return s.fillna(0.0).astype(float)
    uniq = s.unique()
    mapping = {v: clean_currency_str(v) for v in uniq}
    return s.map(mapping).fillna(0.0).astype(float)


def parse_date_series(
    s: pd.Series, default_year: Optional[int] = None
) -> pd.Series:
    """Chuẩn hóa nhanh toàn bộ cột ngày tháng bằng ánh xạ giá trị duy nhất."""
    if s is None or s.empty:
        return pd.Series(dtype=object)
    uniq = s.unique()
    mapping = {v: parse_date_str(v, default_year=default_year) for v in uniq}
    return s.map(mapping)


def is_cancelled_status(status_str: Any) -> bool:
    s = str(status_str).lower().strip()
    if any(
        k in s
        for k in ["huỷ", "hủy", "cancel", "không giao được", "khong giao duoc"]
    ):
        return True
    if re.search(r"\bhuy\b", s):
        return True
    return False


def is_returned_status(status_str: Any) -> bool:
    s = str(status_str).lower().strip()
    if "hoàn thành" in s or "hoan thanh" in s or "thành công" in s:
        return False
    return any(
        k in s
        for k in [
            "trả hàng",
            "đã trả",
            "tra hang",
            "hàng trả",
            "hang tra",
            "trả về",
            "tra ve",
            "hoàn hàng",
            "hoan hang",
            "hoàn trả",
            "hoan tra",
            "đổi trả",
            "doi tra",
            "trả lại",
            "tra lai",
            "khách trả",
            "return",
        ]
    )


def normalize_status_4(status_str: Any) -> str:
    """
    Rulebook §8: Mỗi đơn có đúng 1 trong 4 trạng thái:
    'hoan_thanh', 'huy', 'tra_hang', 'dang_giao'.
    """
    if is_cancelled_status(status_str):
        return "huy"
    if is_returned_status(status_str):
        return "tra_hang"
    s = str(status_str).lower().strip()
    if any(k in s for k in ["đang giao", "dang giao", "chờ giao", "vận chuyển", "đang chở"]):
        return "dang_giao"
    return "hoan_thanh"


class DataLoader:
    """
    Kế thừa từ bài làm nộp thử/datamate/data_loader.py:
    - Tự động đọc README.md, lấy ngày cuối dữ liệu (max_data_date), bảng quy đổi đơn vị, và danh sách cột trống.
    - Đọc tất cả file .csv, .xlsx (gộp sheet theo tháng, xử lý header 2 dòng), .sqlite.
    - Loại bỏ dòng tổng cộng ('Tổng ngày...', 'Tổng cộng', 'Total') TRƯỚC KHI ffill ô bị gộp.
    """

    def __init__(self, biz_path: str) -> None:
        self.biz_path = biz_path
        self.data_files: Dict[str, pd.DataFrame] = {}
        self.unit_conversions: Dict[str, Dict[str, float]] = {}
        self.max_data_date: Optional[str] = None
        self.readme_content: str = ""
        self.readme_empty_cols: Set[str] = set()
        self.is_no_item_detail_shop: bool = False
        self.is_no_customer_shop: bool = False
        self.load_all()

    def load_all(self) -> None:
        readme_path = os.path.join(self.biz_path, "README.md")
        if os.path.exists(readme_path):
            self.parse_readme(readme_path)

        if not os.path.exists(self.biz_path):
            return

        for fname in sorted(os.listdir(self.biz_path)):
            fpath = os.path.join(self.biz_path, fname)
            if fname == "README.md" or fname.startswith("~$") or os.path.isdir(fpath):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext == ".csv":
                self.load_csv(fname, fpath)
            elif ext == ".xlsx":
                self.load_excel(fname, fpath)
            elif ext == ".sqlite":
                self.load_sqlite(fname, fpath)

    def parse_readme(self, readme_path: str) -> None:
        with open(readme_path, mode="r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        self.readme_content = content

        m_date = re.search(r"hết\s+(\d{1,2}[/-]\d{1,2}[/-]\d{4})", content)
        if m_date:
            self.max_data_date = parse_date_str(m_date.group(1))

        if "không có chi tiết mặt hàng" in content.lower():
            self.is_no_item_detail_shop = True
        if "không ghi tên khách" in content.lower():
            self.is_no_customer_shop = True

        for line in content.split("\n"):
            line_s = line.strip()
            # Phát hiện cột trống được ghi trong README: "- **Giới tính**: cột trống, không có dữ liệu"
            m_empty = re.search(
                r"-\s*\*\*(.+?)\*\*:\s*cột trống,\s*không có dữ liệu",
                line_s,
                re.IGNORECASE,
            )
            if m_empty:
                self.readme_empty_cols.add(m_empty.group(1).strip().lower())

            m_conv = re.search(
                r"-\s*(.+?):\s*1\s+([^=]+?)\s*=\s*(\d+)\s+([^\s,;.]+)", line_s
            )
            if m_conv:
                item_name = m_conv.group(1).strip()
                unit_large = m_conv.group(2).strip().lower()
                ratio = float(m_conv.group(3))
                if item_name not in self.unit_conversions:
                    self.unit_conversions[item_name] = {}
                self.unit_conversions[item_name][unit_large] = ratio

        m_inline = re.search(
            r"Hàng theo bó:\s*(.+?)\s*bán theo\s+([^\s,]+),\s*1\s+([^\s=]+)\s*=\s*(\d+)",
            content,
        )
        if m_inline:
            items_part = m_inline.group(1)
            unit_l = m_inline.group(3).strip().lower()
            ratio = float(m_inline.group(4))
            for it in items_part.split(","):
                it_name = it.strip()
                if it_name:
                    if it_name not in self.unit_conversions:
                        self.unit_conversions[it_name] = {}
                    self.unit_conversions[it_name][unit_l] = ratio

    def find_header_row(self, df_raw: pd.DataFrame) -> int:
        best_row = 0
        max_headers = 0
        for r in range(min(10, len(df_raw))):
            row_vals = df_raw.iloc[r].dropna().tolist()
            str_count = 0
            for v in row_vals:
                if isinstance(v, str):
                    vs = v.strip()
                    if (
                        len(vs) >= 2
                        and not re.match(r"^\d+[/-]\d+", vs)
                        and not re.match(r"^\d+([.,]\d+)?$", vs)
                    ):
                        str_count += 1
            if str_count > max_headers and str_count >= 3:
                max_headers = str_count
                best_row = r
        return best_row

    def load_excel(self, fname: str, fpath: str) -> None:
        try:
            xl = pd.ExcelFile(fpath)
            sheets_data = []
            default_yr = int(self.max_data_date[:4]) if self.max_data_date else None
            is_monthly = len(xl.sheet_names) > 1 and all(
                re.search(
                    r"(?:tháng|thg|t\s*\d|\d{1,2}[._/-]202\d)", s, re.IGNORECASE
                )
                for s in xl.sheet_names
            )

            for sheet in xl.sheet_names:
                df_raw = xl.parse(sheet, header=None)
                if df_raw.empty:
                    continue
                header_idx = self.find_header_row(df_raw)
                header = [
                    str(col).strip() if pd.notna(col) else f"col_{i}"
                    for i, col in enumerate(df_raw.iloc[header_idx])
                ]
                df = df_raw.iloc[header_idx + 1 :].copy()
                df.columns = header
                df = df.dropna(how="all")
                if not df.empty:
                    m_yr = re.search(r"202\d", sheet)
                    if not m_yr:
                        m_yr = re.search(r"202\d", fname)
                    sheet_yr = int(m_yr.group(0)) if m_yr else default_yr
                    if sheet_yr:
                        df["_sheet_year"] = sheet_yr
                    df["_sheet_name"] = str(sheet).strip()
                    df = self.normalize_table(df)
                    if is_monthly:
                        sheets_data.append(df)
                    else:
                        if len(xl.sheet_names) == 1:
                            self.data_files[fname] = df
                        else:
                            self.data_files[f"{fname}:{sheet}"] = df

            if is_monthly and sheets_data:
                combined_df = pd.concat(sheets_data, ignore_index=True)
                self.data_files[fname] = combined_df
        except Exception:
            pass

    def load_csv(self, fname: str, fpath: str) -> None:
        try:
            df = pd.read_csv(fpath)
            df = self.normalize_table(df)
            self.data_files[fname] = df
        except Exception:
            pass

    def load_sqlite(self, fname: str, fpath: str) -> None:
        try:
            conn = sqlite3.connect(fpath)
            tables = [
                t[0]
                for t in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
            for tbl in tables:
                df = pd.read_sql_query(f"SELECT * FROM {tbl}", conn)
                df = self.normalize_table(df)
                self.data_files[f"{fname}:{tbl}"] = df
            conn.close()
        except Exception:
            pass

    def normalize_table(self, df: pd.DataFrame) -> pd.DataFrame:
        df.columns = [str(c).strip() for c in df.columns]

        # 1. Xóa dòng tổng kết TRƯỚC khi ffill (vector hóa trên các cột chuỗi đầu tiên)
        summary_pat = re.compile(
            r"^(tổng(\s+(ngày|tháng|quý|năm|cộng|kết|tiền|số|hđ|đơn|bill|phiếu))?|cộng(\s+(ngày|tháng|quý|năm))?|total)(\s|$|:)",
            re.IGNORECASE,
        )
        str_cols = [c for c in df.columns if not c.startswith("_")]
        if str_cols and not df.empty:
            mask = pd.Series(False, index=df.index)
            for c in str_cols[:5]:
                if pd.api.types.is_numeric_dtype(df[c]):
                    continue
                s_col = df[c].dropna()
                if s_col.empty:
                    continue
                uniq_strs = [v for v in s_col.unique() if isinstance(v, str) and summary_pat.match(v.strip())]
                if uniq_strs:
                    mask = mask | df[c].isin(uniq_strs)
            if mask.any():
                df = df[~mask].copy()

        # 2. Rulebook §8 (Mục 142): ffill các cột cấp đơn hàng bị gộp ô
        ffill_keywords = [
            "mã đơn",
            "ma don",
            "mã hóa đơn",
            "mã hoá đơn",
            "mã hđ",
            "số hđ",
            "mã bill",
            "mã phiếu",
            "số phiếu",
            "số ct",
            "ngày đặt",
            "ngày bán",
            "ngày lập",
            "ngày giao dịch",
            "ngày tháng",
            "ngày xuất",
            "trạng thái",
            "tình trạng",
            "kết quả",
            "tiến độ",
            "xử lý",
            "status",
            "tt đơn",
            "khách hàng",
            "tenkh",
            "tên kh",
            "họ tên khách",
            "đối tác",
            "bên mua",
        ]
        exact_ffill = {
            "hđ",
            "hd",
            "bill",
            "ngày",
            "ngay",
            "date",
            "thời gian",
            "khách",
            "kh",
        }
        for c in df.columns:
            c_l = c.lower().strip()
            if c_l in exact_ffill or any(k in c_l for k in ffill_keywords):
                if df[c].isna().any() and df[c].notna().any():
                    df[c] = df[c].ffill()

        return df


class BusinessAdapter:
    """
    Kế thừa 100% logic chuẩn hóa đa bảng từ bài làm nộp thử/datamate/business_adapter.py,
    và bổ sung các cột chuẩn hóa cho V['sales'] và V['orders'] phục vụ Code-Act Agent.
    """

    def __init__(self, data_loader: DataLoader) -> None:
        self.loader = data_loader
        self.orders_df: Optional[pd.DataFrame] = None
        self.items_df: Optional[pd.DataFrame] = None
        self.products_df: Optional[pd.DataFrame] = None
        self.customers_df: Optional[pd.DataFrame] = None
        self.imports_df: Optional[pd.DataFrame] = None
        self.expenses_df: Optional[pd.DataFrame] = None
        self.inventory_df: Optional[pd.DataFrame] = None
        self.returned_order_ids: Set[str] = set()
        self.abbrev_map: Dict[str, str] = {}
        self.is_order_summary_only: bool = False
        self.unit_conversions: Dict[str, Dict[str, float]] = {
            k: v.copy() for k, v in data_loader.unit_conversions.items()
        }
        self.normalize()

    def find_col(self, cols: List[str], patterns: List[str]) -> Optional[str]:
        for pat in patterns:
            for col in cols:
                if re.fullmatch(pat, col.strip().lower()):
                    return col
        for pat in patterns:
            for col in cols:
                if re.search(pat, col.strip().lower()):
                    return col
        return None

    def normalize(self) -> None:
        # 0. Bảng ghi tắt (dev_011, dev_014)
        for fname, df in self.loader.data_files.items():
            fname_l = fname.lower()
            if "ghi tắt" in fname_l or "ghitat" in fname_l or "ghi_tat" in fname_l:
                self.extract_abbreviations(df)

        # 1. Bảng quy đổi đơn vị
        for fname, df in self.loader.data_files.items():
            fname_l = fname.lower()
            if any(
                k in fname_l
                for k in ["quydoi", "donviquydoi", "don_vi_sp", "donvitinh", "danhmuc", "danh_muc"]
            ):
                self.extract_conversions(df)

        # 2. Danh mục sản phẩm / hàng hóa
        for fname, df in self.loader.data_files.items():
            fname_l = fname.lower()
            if any(
                k in fname_l
                for k in [
                    "sanpham",
                    "san_pham",
                    "hanghoa",
                    "hang_hoa",
                    "danhmuc",
                    "danh_muc",
                    "danh_sach_hang_hoa",
                    "hàng hóa",
                    "h├áng h├│a",
                ]
            ):
                self.extract_products(df)

        # 3. Thông tin thuốc
        for fname, df in self.loader.data_files.items():
            fname_l = fname.lower()
            if "thuoc" in fname_l:
                self.extract_medicine_info(df)

        # 3b. Bảng trả hàng / đơn hàng lô có số lượng trả
        for fname, df in self.loader.data_files.items():
            fname_l = fname.lower()
            if any(
                k in fname_l
                for k in [
                    "donhang_lo",
                    "chi_tiet_don_lo",
                    "chitietlo",
                    "bantheolo",
                    "ban_theo_lo",
                    "tra_hang",
                    "trahang",
                ]
            ):
                self.extract_returned_orders(fname_l, df)

        # 3c. Bảng tồn kho & khách hàng
        for fname, df in self.loader.data_files.items():
            fname_l = fname.lower()
            if any(k in fname_l for k in ["tonkho", "ton_kho"]):
                self.extract_inventory(df)
            elif any(k in fname_l for k in ["khachhang", "khach_hang"]):
                self.extract_customers(df)

        # 4. Bảng bán hàng, nhập hàng, chi phí
        items_dfs = []
        orders_dfs = []
        imports_dfs = []
        expenses_dfs = []

        non_sales_keywords = [
            "quydoi",
            "donviquydoi",
            "don_vi_sp",
            "donvitinh",
            "sanpham",
            "san_pham",
            "hanghoa",
            "hang_hoa",
            "danhmuc",
            "danh_muc",
            "danh_sach_hang_hoa",
            "hàng hóa",
            "h├áng h├│a",
            "thuoc",
            "tonkho",
            "ton_kho",
            "the_kho",
            "lo_hang",
            "lohang",
            "donhang_lo",
            "chi_tiet_don_lo",
            "chitietlo",
            "bantheolo",
            "ban_theo_lo",
            "phongban",
            "phong_ban",
            "banggia",
            "bang_gia",
            "lichsugia",
            "lich_su_gia",
            "giaohang",
            "giao_hang",
            "giao hàng",
            "giao h├áng",
            "phiếu giao",
            "hanno",
            "han_no",
            "công nợ",
            "c├┤ng nß╗ú",
            "theo hạn",
            "sono",
            "so_no",
            "sogop",
            "so_gop",
            "phieuthu",
            "phieu_thu",
            "thanhtoan",
            "thanh_toan",
            "soquy",
            "so_quy",
            "baocaoca",
            "bao_cao_ca",
            "chi_nhanh",
            "khachhang",
            "khach_hang",
            "nhanvien",
            "nhan_vien",
            "danhsachtho",
            "nhacungcap",
            "nha_cung_cap",
            "khuyenmai",
            "khuyen_mai",
            "thumaycu",
            "thu_may_cu",
            "baohanh",
            "bao_hanh",
            "tra_hang",
            "trahang",
            "ghi tắt",
            "ghitat",
            "ghi_tat",
        ]

        for fname, df in self.loader.data_files.items():
            fname_l = fname.lower()
            if "chiphi" in fname_l or "chi_phi" in fname_l:
                exp_df = self.extract_expenses(df)
                if exp_df is not None:
                    expenses_dfs.append(exp_df)
                continue

            if any(
                k in fname_l
                for k in [
                    "nhaphang",
                    "nhap_hang",
                    "phieu_nhap",
                    "chi_tiet_nhap",
                    "donnhaphang",
                ]
            ):
                imp_df = self.extract_imports(fname_l, df)
                if imp_df is not None:
                    imports_dfs.append(imp_df)
                continue

            if any(k in fname_l for k in non_sales_keywords):
                continue

            order_data, item_data = self.extract_sales_table(df)
            if item_data is not None:
                items_dfs.append(item_data)
                if order_data is not None:
                    orders_dfs.append(order_data)
            elif order_data is not None:
                # Ưu tiên bảng header đơn hàng (don_hang, DanhSachHoaDon) lên đầu
                orders_dfs.insert(0, order_data)

        if orders_dfs:
            combined_ord = pd.concat(orders_dfs, ignore_index=True)
            if "order_id" in combined_ord.columns:
                combined_ord["_has_dt"] = combined_ord["date"].notna() & (
                    combined_ord["date"] != ""
                )
                combined_ord["_has_tot"] = combined_ord["total_order"] > 0
                combined_ord = (
                    combined_ord.sort_values(
                        by=["_has_dt", "_has_tot"],
                        ascending=[False, False],
                        kind="mergesort",
                    )
                    .drop_duplicates(subset=["order_id"], keep="first")
                    .drop(columns=["_has_dt", "_has_tot"])
                    .copy()
                )
                self.orders_df = combined_ord
            else:
                self.orders_df = combined_ord.drop_duplicates().copy()

        if items_dfs:
            combined = pd.concat(items_dfs, ignore_index=True)
            # Rulebook §8 (Mục 145 & 146):
            # - Hai dòng của cùng một đơn giống nhau ở mọi ô là lỗi xuất file -> tính 1 lần.
            # - Bảng không ghi theo đơn -> hai dòng giống nhau vẫn tính cả hai.
            if "_has_order_id" in combined.columns and combined["_has_order_id"].all():
                dedup_cols = [
                    c
                    for c in [
                        "order_id",
                        "item_code",
                        "item_name",
                        "unit",
                        "qty",
                        "unit_price",
                        "amount",
                    ]
                    if c in combined.columns
                ]
                if dedup_cols:
                    self.items_df = combined.drop_duplicates(
                        subset=dedup_cols, keep="first"
                    ).copy()
                else:
                    self.items_df = combined.copy()
            else:
                self.items_df = combined.copy()
        elif self.orders_df is not None and not self.orders_df.empty:
            self.is_order_summary_only = True
            synth = self.orders_df.copy()
            synth["item_name"] = "Đơn hàng"
            synth["item_code"] = "ORDER"
            synth["unit"] = "đơn"
            synth["qty"] = 1.0
            synth["qty_base"] = 1.0
            synth["unit_price"] = synth["total_order"]
            synth["amount"] = synth["total_order"]
            synth["cost_price"] = 0.0
            self.items_df = synth

        if imports_dfs:
            self.imports_df = pd.concat(imports_dfs, ignore_index=True)
        if expenses_dfs:
            self.expenses_df = pd.concat(expenses_dfs, ignore_index=True)

        self.link_orders_and_items()
        self.enrich_views()

    def extract_abbreviations(self, df: pd.DataFrame) -> None:
        cols = list(df.columns)
        c_short = self.find_col(
            cols, [r"ghi tắt", r"viết tắt", r"mã tắt", r"tên tắt", r"cách ghi"]
        )
        c_full = self.find_col(
            cols, [r"tên đầy đủ", r"tên hàng", r"tên sản phẩm", r"đầy đủ"]
        )
        if c_short and c_full:
            for _, row in df.iterrows():
                s = str(row[c_short]).strip()
                f = str(row[c_full]).strip()
                if s and f and s.lower() != "nan" and f.lower() != "nan":
                    self.abbrev_map[s.lower()] = f

    def extract_returned_orders(self, fname_l: str, df: pd.DataFrame) -> None:
        cols = list(df.columns)
        c_ord = self.find_col(
            cols,
            [r"mã đơn hàng", r"mã đơn", r"madon", r"ma_don", r"số đơn", r"mã hđ"],
        )
        if not c_ord:
            return
        if "tra_hang" in fname_l or "trahang" in fname_l:
            for val in df[c_ord].dropna().unique():
                self.returned_order_ids.add(str(val).strip())
        else:
            c_ret_qty = self.find_col(
                cols, [r"số lượng trả", r"sl trả", r"sl_tra", r"sltra"]
            )
            if c_ret_qty:
                ret_rows = df[df[c_ret_qty].apply(clean_currency_str) > 0]
                for val in ret_rows[c_ord].dropna().unique():
                    self.returned_order_ids.add(str(val).strip())

    def extract_conversions(self, df: pd.DataFrame) -> None:
        cols = list(df.columns)
        c_inline = self.find_col(cols, [r"^quy đổi$", r"^quy_doi$"])
        p_name = self.find_col(
            cols,
            [
                r"tên sản phẩm",
                r"tên hàng hóa",
                r"tên hàng",
                r"tên món",
                r"sản phẩm",
                r"^hàng$",
                r"mã sp",
                r"mã hàng",
                r"masp",
            ],
        )
        if c_inline and p_name:
            for _, row in df.iterrows():
                p = str(row[p_name]).strip()
                val = str(row[c_inline]).strip()
                m = re.search(r"1\s+([^\s=]+)\s*=\s*(\d+)\s+([^\s]+)", val)
                if m and p and p.lower() != "nan":
                    u_l = m.group(1).strip().lower()
                    rate = float(m.group(2))
                    if p not in self.unit_conversions:
                        self.unit_conversions[p] = {}
                    self.unit_conversions[p][u_l] = rate

        u_conv = self.find_col(
            cols,
            [
                r"đơn vị quy đổi",
                r"đvt quy đổi",
                r"tên đvt",
                r"loại đv",
                r"đv nhập",
                r"ten_dvt",
                r"tendvt",
                r"đơn vị lớn",
                r"đơn vị",
            ],
        )
        u_rate = self.find_col(
            cols,
            [
                r"tỉ lệ quy đổi",
                r"tỷ lệ quy đổi",
                r"hệ số quy đổi",
                r"hệ số",
                r"giá trị quy đổi",
                r"he_so",
                r"heso",
                r"bằng",
            ],
        )
        if p_name and u_conv and u_rate:
            raw_entries = []
            for _, row in df.iterrows():
                p = str(row[p_name]).strip()
                unit = str(row[u_conv]).strip().lower()
                rate_raw = str(row[u_rate]).strip().lower()
                if not p or p.lower() == "nan" or not unit or unit == "nan":
                    continue
                if p not in self.unit_conversions:
                    self.unit_conversions[p] = {}
                try:
                    rate = float(rate_raw)
                    self.unit_conversions[p][unit] = rate
                except Exception:
                    m_rel = re.match(r"^(\d+(?:[.,]\d+)?)\s+(.+)$", rate_raw)
                    if m_rel:
                        raw_entries.append(
                            (
                                p,
                                unit,
                                float(m_rel.group(1).replace(",", ".")),
                                m_rel.group(2).strip(),
                            )
                        )
            for p, unit, mult, sub_unit in raw_entries:
                sub_rate = self.unit_conversions.get(p, {}).get(sub_unit, 1.0)
                self.unit_conversions[p][unit] = mult * sub_rate

    def extract_products(self, df: pd.DataFrame) -> None:
        cols = list(df.columns)
        c_code = self.find_col(
            cols,
            [
                r"mã sku",
                r"sku",
                r"mã hàng hóa",
                r"mahanghoa",
                r"mã hàng",
                r"mã hh",
                r"mã sản phẩm bán",
                r"mã sản phẩm",
                r"mã vạch",
                r"ma_sp",
                r"masp",
                r"id sp",
                r"ma_hang",
            ],
        )
        c_name = self.find_col(
            cols,
            [
                r"tên hàng hóa",
                r"tên sản phẩm",
                r"tên sp",
                r"tên hàng",
                r"ten_hang",
                r"tensp",
                r"ten_sp",
                r"tengoi",
                r"hàng bán",
                r"tên món",
                r"sản phẩm",
                r"món",
                r"^hàng$",
            ],
        )
        c_group = self.find_col(
            cols,
            [
                r"nhóm hàng hóa",
                r"nhóm hàng",
                r"nhóm",
                r"nhóm hàng\(3 cấp\)",
                r"nhom hang",
                r"nhom_hang",
                r"nhomsp",
                r"danh mục",
                r"danhmuc",
                r"loại sản phẩm",
                r"loại hàng",
                r"phân loại",
            ],
        )
        c_unit = self.find_col(
            cols,
            [
                r"đơn vị tính",
                r"đơn vị cơ bản",
                r"đvt cơ bản",
                r"đơn vị gốc",
                r"đv gốc",
                r"dvt_co_ban",
                r"dvt",
                r"đvt",
                r"đơn vị",
            ],
        )
        c_price = self.find_col(
            cols, [r"giá bán", r"giá hiện tại", r"giá niêm yết", r"gia_ban", r"giá"]
        )
        c_cost = self.find_col(
            cols, [r"giá vốn", r"giá vốn hiện tại", r"gia_von", r"giá nhập"]
        )
        c_size = self.find_col(cols, [r"số / size", r"size", r"cỡ"])

        res = pd.DataFrame()
        if c_code:
            res["item_code"] = df[c_code].fillna("").astype(str).str.strip()
        elif c_name:
            res["item_code"] = df[c_name].fillna("").astype(str).str.strip()
        else:
            return

        res["item_name"] = (
            df[c_name].fillna("").astype(str).str.strip()
            if c_name
            else res["item_code"]
        )
        res["item_group"] = (
            df[c_group].fillna("").astype(str).str.strip() if c_group else ""
        )
        res["base_unit"] = (
            df[c_unit].fillna("").astype(str).str.strip().str.lower()
            if c_unit
            else ""
        )
        res["price"] = df[c_price].apply(clean_currency_str) if c_price else 0.0
        res["cost_price"] = df[c_cost].apply(clean_currency_str) if c_cost else 0.0
        res["_has_catalog_cost"] = bool(c_cost is not None)
        if c_size:
            res["size_number"] = df[c_size].fillna("").astype(str).str.strip()

        if self.products_df is None:
            self.products_df = res
        else:
            self.products_df = (
                pd.concat([self.products_df, res], ignore_index=True)
                .drop_duplicates(subset=["item_code"], keep="first")
                .copy()
            )

    def extract_medicine_info(self, df: pd.DataFrame) -> None:
        cols = list(df.columns)
        c_code = self.find_col(
            cols,
            [
                r"mã sku",
                r"sku",
                r"mã hàng",
                r"mahanghoa",
                r"mã hh",
                r"mã sản phẩm",
                r"masp",
                r"ma_sp",
            ],
        )
        c_active = self.find_col(
            cols,
            [
                r"hoạt chất",
                r"hoạt chất chính",
                r"hoatchat",
                r"hoat_chat",
                r"thanhphanchinh",
            ],
        )
        c_rx = self.find_col(
            cols, [r"thuốc kê đơn", r"kê đơn", r"bantheodon", r"ban_theo_don"]
        )

        if not c_code:
            return

        med_df = pd.DataFrame()
        med_df["item_code"] = df[c_code].fillna("").astype(str).str.strip()
        med_df["active_ingredient"] = (
            df[c_active].fillna("").astype(str).str.strip() if c_active else ""
        )
        if c_rx:
            med_df["is_prescription"] = df[c_rx].apply(
                lambda x: str(x).strip().lower()
                in ["true", "1", "1.0", "có", "yes", "kê đơn"]
            )
        else:
            med_df["is_prescription"] = False

        if self.products_df is not None:
            for col in ["active_ingredient", "is_prescription"]:
                if col in self.products_df.columns:
                    self.products_df = self.products_df.drop(columns=[col])
            self.products_df = pd.merge(
                self.products_df, med_df, on="item_code", how="left"
            )
        else:
            self.products_df = med_df

    def extract_inventory(self, df: pd.DataFrame) -> None:
        cols = list(df.columns)
        c_code = self.find_col(
            cols,
            [
                r"mã hàng",
                r"mahanghoa",
                r"mã sản phẩm",
                r"mã sku",
                r"id sp",
                r"ma_sp",
                r"masp",
                r"ma_hang",
            ],
        )
        c_name = self.find_col(
            cols,
            [
                r"tên hàng",
                r"tên sản phẩm",
                r"hàng hóa",
                r"ten_sp",
                r"ten_hang",
                r"tengoi",
                r"sản phẩm",
            ],
        )
        c_qty = self.find_col(
            cols,
            [
                r"tồn cuối kỳ",
                r"toncuoi",
                r"tồn kho",
                r"ton kho",
                r"ton_kho",
                r"tonkho",
                r"số lượng tồn",
                r"tồn",
            ],
        )
        if not c_qty or (not c_code and not c_name):
            return
        inv = pd.DataFrame()
        inv["item_code"] = (
            df[c_code].fillna("").astype(str).str.strip()
            if c_code
            else df[c_name].fillna("").astype(str).str.strip()
        )
        inv["item_name"] = (
            df[c_name].fillna("").astype(str).str.strip()
            if c_name
            else inv["item_code"]
        )
        inv["qty_base"] = clean_currency_series(df[c_qty])
        self.inventory_df = inv

    def extract_customers(self, df: pd.DataFrame) -> None:
        cols = list(df.columns)
        c_id = self.find_col(
            cols,
            [
                r"mã khách hàng",
                r"makhachhang",
                r"customer id",
                r"mã khách lẻ/sỉ",
                r"mã kh",
                r"ma_kh",
                r"makh",
                r"mã thẻ",
            ],
        )
        c_name = self.find_col(
            cols,
            [
                r"tên khách hàng",
                r"khách hàng \(tên\)",
                r"họ tên",
                r"tên kh",
                r"ten_kh",
                r"tengoi",
                r"khách hàng",
                r"^kh$",
            ],
        )
        c_group = self.find_col(
            cols,
            [
                r"nhóm khách",
                r"nhom_kh",
                r"hạng thẻ",
                r"^hạng$",
                r"loại khách",
                r"loaikhachhang",
                r"loại hình khách",
                r"loai_hinh_kh",
                r"^nhóm$",
            ],
        )
        c_addr = self.find_col(
            cols, [r"địa chỉ", r"dia_chi", r"diachilienhe", r"đường", r"quận"]
        )
        c_gender = self.find_col(cols, [r"giới tính", r"gioi_tinh", r"namnu", r"phái"])
        c_reg = self.find_col(
            cols, [r"ngày đăng ký", r"ngay_dang_ky", r"ngày tạo", r"taoluc"]
        )
        if not c_id and not c_name:
            return
        cdf = pd.DataFrame()
        cdf["cust_id"] = (
            df[c_id].fillna("").astype(str).str.strip()
            if c_id
            else df[c_name].fillna("").astype(str).str.strip()
        )
        cdf["cust_name"] = (
            df[c_name].fillna("").astype(str).str.strip() if c_name else cdf["cust_id"]
        )
        if c_group:
            cdf["cust_group"] = df[c_group].fillna("").astype(str).str.strip()
        if c_addr and c_addr.lower() not in self.loader.readme_empty_cols:
            cdf["address"] = df[c_addr].fillna("").astype(str).str.strip()
        if c_gender and c_gender.lower() not in self.loader.readme_empty_cols:
            cdf["gender"] = df[c_gender].fillna("").astype(str).str.strip()
        if c_reg:
            cdf["reg_date"] = df[c_reg].apply(parse_date_str)
        self.customers_df = cdf

    def extract_sales_table(
        self, df: pd.DataFrame
    ) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame]]:
        cols = list(df.columns)

        # Chốt chặn cấp cột: loại bỏ bảng giao hàng, bảng hạn nợ, bảng phòng/bàn, bảng lô hàng
        if self.find_col(
            cols,
            [
                r"ngày giao hàng",
                r"lần giao",
                r"số lượng giao",
                r"hạn thanh toán",
                r"số ngày được nợ",
                r"phòng/bàn",
                r"số lượng trả",
                r"hạn sử dụng",
                r"^lô$",
            ],
        ):
            return None, None

        c_order_id = self.find_col(
            cols,
            [
                r"mã đơn hàng",
                r"mã hóa đơn",
                r"mã hoá đơn",
                r"mahoadon",
                r"mã đơn",
                r"order id",
                r"mã bill",
                r"^bill$",
                r"ma don",
                r"ma_don",
                r"madon",
                r"số hđ",
                r"mã chứng từ",
                r"số ct",
                r"mã giao dịch",
                r"mã hđ",
                r"so_hd",
                r"mahd",
                r"ma_hd",
                r"số đơn",
                r"invoice no",
                r"^hđ$",
                r"^hd$",
                r"^số_hđ$",
                r"^mã_hđ$",
                r"số phiếu",
                r"mã phiếu",
            ],
        )
        if not c_order_id and not self.find_col(cols, [r"^no\.$", r"số dòng"]):
            c_order_id = self.find_col(cols, [r"^stt$"])

        c_date = self.find_col(
            cols,
            [
                r"ngày đặt hàng",
                r"ngày bán",
                r"ngày giao dịch",
                r"ngày tháng",
                r"ngày xuất",
                r"thời gian tạo",
                r"thời gian",
                r"ngayban",
                r"ngay_ban",
                r"ngaydat",
                r"ngay_dat",
                r"ngày lập",
                r"ngaylap",
                r"thoigianlap",
                r"ngày hđ",
                r"ngày chứng từ",
                r"ngày sửa",
                r"ngày",
                r"ngay",
                r"^date$",
            ],
        )
        c_status = self.find_col(
            cols,
            [
                r"trạng thái đơn hàng",
                r"trạng thái đơn",
                r"trạng thái xử lý",
                r"trạng thái hđ",
                r"trạng thái",
                r"trang_thai",
                r"trangthai",
                r"kết quả",
                r"tiến độ",
                r"xử lý",
                r"status",
                r"tình trạng đơn",
                r"tình trạng",
                r"tinhtrang",
                r"tt đơn",
                r"ghi chú tt",
                r"ghi chú",
                r"ghichu",
            ],
        )
        c_item_name = self.find_col(
            cols,
            [
                r"tên hàng hóa",
                r"tên sản phẩm",
                r"tên sp",
                r"tên hàng",
                r"tên mặt hàng",
                r"mặt hàng",
                r"hàng bán",
                r"chi tiết hàng",
                r"sản phẩm",
                r"món",
                r"nội dung",
                r"hàng hóa",
                r"^hàng$",
                r"ten_hang",
                r"ten_sp",
                r"tensp",
                r"làm gì",
                r"dịch vụ",
            ],
        )
        if c_item_name and c_item_name.lower() in self.loader.readme_empty_cols:
            c_item_name = None

        c_item_code = self.find_col(
            cols,
            [
                r"mã hàng hóa",
                r"mahanghoa",
                r"mã hàng",
                r"mã mặt hàng",
                r"mã sản phẩm",
                r"mã h\.hóa",
                r"mã vạch",
                r"ma hang",
                r"ma_hang",
                r"mahang",
                r"ma_sp",
                r"masp",
                r"id sp",
                r"sku",
                r"mã sp",
                r"^mã$",
            ],
        )
        c_qty = self.find_col(
            cols,
            [
                r"số lượng sản phẩm",
                r"số lượng bán",
                r"số lượng",
                r"sl bán",
                r"sluong",
                r"soluong",
                r"so_luong",
                r"slg",
                r"^sl$",
                r"^s\.l$",
            ],
        )
        c_qty_base = self.find_col(
            cols,
            [
                r"số lượng quy đổi",
                r"quy ra đv lẻ",
                r"sl quy đổi",
                r"sl_quy_doi",
                r"slquydoi",
                r"sl cơ bản",
                r"số lượng lẻ",
                r"sl lẻ",
                r"sl quy ra lẻ",
            ],
        )
        c_unit = self.find_col(
            cols,
            [
                r"đơn vị tính",
                r"donvitinh",
                r"đơn vị \(tính\)",
                r"đv tính",
                r"đvt",
                r"dvt",
                r"đơn vị",
                r"đ\.vị",
                r"unit",
                r"^đvt$",
                r"^dvt$",
            ],
        )
        c_price = self.find_col(
            cols,
            [
                r"giá sản phẩm",
                r"đơn giá \(đ\)",
                r"đơn giá",
                r"giá 1 đv",
                r"giá bán",
                r"giá đơn vị",
                r"giadonvi",
                r"giá niêm yết",
                r"don_gia",
                r"dongia",
                r"giá lẻ",
                r"giá",
                r"^đg$",
                r"^dg$",
                r"tiền công",
            ],
        )
        c_amount = self.find_col(
            cols,
            [
                r"thành tiền sau giảm",
                r"thành tiền vnd",
                r"thành tiền",
                r"thanh tien",
                r"thanh_tien",
                r"thanhtien",
                r"tiền bán",
                r"t\.tiền",
                r"trị giá",
                r"trigia",
                r"tri_gia",
                r"^amount$",
                r"^cộng$",
                r"^tt$",
                r"^tiền$",
                r"^tổng$",
                r"giá trị hàng",
                r"tổng tiền",
            ],
        )
        c_tien_hang = self.find_col(cols, [r"^tien hang$", r"^tiền hàng$"])
        c_has_disc = self.find_col(
            cols, [r"giảm giá", r"chiết khấu", r"chietkhau"]
        )
        if c_tien_hang and not c_has_disc:
            c_total_order = c_tien_hang
        else:
            c_total_order = self.find_col(
                cols,
                [
                    r"tổng sau giảm giá",
                    r"thành tiền sau giảm",
                    r"phải thu",
                    r"tổng thanh toán",
                    r"tongthanhtoan",
                    r"tổng đơn",
                    r"tổng bill",
                    r"tổng phiếu",
                    r"giá trị hàng",
                    r"tổng tiền hàng",
                    r"tongtienhang",
                    r"tong tien",
                    r"tongtien",
                    r"tổng tiền",
                    r"tổng cộng",
                    r"thành tiền",
                    r"^tiền$",
                ],
            )
        c_cust_id = self.find_col(
            cols,
            [
                r"mã khách hàng",
                r"makhachhang",
                r"mã khách mua",
                r"mã người mua",
                r"mã kh mua",
                r"mã kh",
                r"mã thẻ",
                r"customer id",
                r"ma_kh",
                r"makh",
                r"số kh",
            ],
        )
        c_cust_name = self.find_col(
            cols,
            [
                r"tên khách hàng",
                r"khách hàng \(tên\)",
                r"họ tên khách",
                r"khách hàng",
                r"tên kh",
                r"đối tác",
                r"bên mua",
                r"ten_kh",
                r"tenkh",
                r"khách",
                r"^kh$",
                r"xe",
                r"biển số",
            ],
        )
        c_payment = self.find_col(
            cols,
            [
                r"phương thức tt",
                r"phuongthuctt",
                r"thanh toán bằng",
                r"trả bằng",
                r"phương thức",
                r"hình thức thanh toán",
                r"hinh_thuc_tt",
                r"pttt",
                r"httt",
            ],
        )
        c_channel = self.find_col(
            cols,
            [
                r"kênh bán",
                r"kenhbanhang",
                r"kênh",
                r"kenh_ban",
                r"kenh",
                r"nguồn đơn",
                r"bán qua",
                r"ban_qua",
                r"loại bán",
                r"loai_ban",
            ],
        )
        c_staff = self.find_col(
            cols,
            [
                r"nhân viên bán",
                r"nhân viên",
                r"nhan_vien",
                r"manhanvien",
                r"người bán",
                r"thu ngân",
                r"thợ",
                r"mã nv",
                r"ten_nv",
            ],
        )
        c_debt = self.find_col(
            cols,
            [
                r"còn nợ",
                r"con no",
                r"con_no",
                r"conphaithu",
                r"dư nợ",
                r"chưa trả",
                r"^nợ$",
                r"còn lại",
            ],
        )
        c_paid = self.find_col(
            cols,
            [
                r"khách đã trả",
                r"khachdatra",
                r"khách trả",
                r"^da tra$",
                r"^đã trả$",
                r"đã thu",
                r"đã thanh toán",
                r"^thanh toán$",
            ],
        )
        c_cost = self.find_col(
            cols, [r"giá vốn lúc bán", r"giavonlucban", r"giá vốn", r"gia_von"]
        )
        c_note = self.find_col(cols, [r"ghi chú", r"ghichu", r"note"])
        c_time = self.find_col(cols, [r"^giờ$", r"giờ bán", r"giờ đặt", r"^gio$"])

        if not c_order_id and not c_date:
            return None, None

        order_df = pd.DataFrame()
        has_explicit_order_id = c_order_id is not None
        if c_order_id:
            if c_order_id.strip().lower() == "stt" and "_sheet_name" in df.columns:
                order_df["order_id"] = (
                    df["_sheet_name"].fillna("").astype(str).str.strip()
                    + "_"
                    + df[c_order_id].fillna("").astype(str).str.strip()
                )
            else:
                order_df["order_id"] = (
                    df[c_order_id].fillna("").astype(str).str.strip()
                )
        else:
            order_df["order_id"] = [f"auto_{i}" for i in range(len(df))]

        fallback_yr = None
        if "_sheet_year" in df.columns:
            valid_yrs = df["_sheet_year"].dropna()
            if not valid_yrs.empty:
                fallback_yr = int(valid_yrs.iloc[0])
        if not fallback_yr and self.loader.max_data_date:
            fallback_yr = int(self.loader.max_data_date[:4])

        if c_date:
            if "_sheet_year" in df.columns and df["_sheet_year"].nunique() > 1:
                pair_uniq = df[[c_date, "_sheet_year"]].drop_duplicates()
                pair_map = {
                    (v, sy): parse_date_str(
                        v, default_year=(int(sy) if pd.notna(sy) else fallback_yr)
                    )
                    for v, sy in zip(pair_uniq[c_date], pair_uniq["_sheet_year"])
                }
                order_df["date"] = [
                    pair_map.get((v, sy))
                    for v, sy in zip(df[c_date], df["_sheet_year"])
                ]
            else:
                order_df["date"] = parse_date_series(
                    df[c_date], default_year=fallback_yr
                )
            if c_time and c_time != c_date:
                order_df["raw_date"] = (
                    df[c_date].fillna("").astype(str)
                    + " "
                    + df[c_time].fillna("").astype(str)
                )
            else:
                order_df["raw_date"] = df[c_date].fillna("").astype(str)
        else:
            order_df["date"] = None
            order_df["raw_date"] = (
                df[c_time].fillna("").astype(str) if c_time else ""
            )

        if c_status and c_note and c_status != c_note:
            s_col = df[c_status].fillna("").astype(str).str.strip()
            n_col = df[c_note].fillna("").astype(str).str.strip()
            order_df["status"] = [
                f"{s} | {n}" if n else s for s, n in zip(s_col, n_col)
            ]
        elif c_status:
            order_df["status"] = df[c_status].fillna("").astype(str).str.strip()
        else:
            order_df["status"] = None

        order_df["cust_id"] = (
            df[c_cust_id].fillna("").astype(str).str.strip() if c_cust_id else ""
        )
        order_df["cust_name"] = (
            df[c_cust_name].fillna("").astype(str).str.strip() if c_cust_name else ""
        )
        order_df["payment_method"] = (
            df[c_payment].fillna("").astype(str).str.strip() if c_payment else ""
        )
        order_df["channel"] = (
            df[c_channel].fillna("").astype(str).str.strip() if c_channel else ""
        )
        order_df["staff"] = (
            df[c_staff].fillna("").astype(str).str.strip() if c_staff else ""
        )
        order_df["total_order"] = (
            clean_currency_series(df[c_total_order]) if c_total_order else 0.0
        )
        order_df["paid"] = clean_currency_series(df[c_paid]) if c_paid else 0.0
        if c_debt:
            order_df["debt"] = clean_currency_series(df[c_debt])
        elif c_total_order and c_paid:
            order_df["debt"] = (order_df["total_order"] - order_df["paid"]).clip(
                lower=0.0
            )
        else:
            order_df["debt"] = 0.0

        item_df = pd.DataFrame()
        item_df["order_id"] = order_df["order_id"]
        item_df["_has_order_id"] = has_explicit_order_id
        item_df["date"] = order_df["date"]
        item_df["raw_date"] = order_df["raw_date"]
        item_df["status"] = order_df["status"]
        item_df["cust_id"] = order_df["cust_id"]
        item_df["cust_name"] = order_df["cust_name"]
        item_df["payment_method"] = order_df["payment_method"]
        item_df["channel"] = order_df["channel"]
        item_df["staff"] = order_df["staff"]

        if c_item_name:
            item_df["item_name"] = (
                df[c_item_name]
                .fillna("")
                .astype(str)
                .str.replace(r"\s+", " ", regex=True)
                .str.strip()
            )
        elif c_item_code:
            item_df["item_name"] = (
                df[c_item_code]
                .fillna("")
                .astype(str)
                .str.replace(r"\s+", " ", regex=True)
                .str.strip()
            )
        else:
            return order_df, None

        if self.abbrev_map:
            item_df["item_name"] = item_df["item_name"].apply(
                lambda x: self.abbrev_map.get(str(x).lower().strip(), x)
            )

        item_df["item_code"] = (
            df[c_item_code]
            .fillna("")
            .astype(str)
            .str.replace(r"\s+", " ", regex=True)
            .str.strip()
            if c_item_code
            else item_df["item_name"]
        )
        item_df["unit"] = (
            df[c_unit].fillna("").astype(str).str.strip().str.lower()
            if c_unit
            else ""
        )
        item_df["qty"] = clean_currency_series(df[c_qty]) if c_qty else 1.0

        # Đơn có dòng số lượng âm là đơn có trả hàng (Rulebook §1 & dev_004/biz_105)
        neg_qty_mask = item_df["qty"] < 0
        if neg_qty_mask.any():
            for oid in item_df.loc[neg_qty_mask, "order_id"].dropna().unique():
                self.returned_order_ids.add(str(oid).strip())
            item_df = item_df[~neg_qty_mask].copy()

        # Lọc bỏ các dòng điều chỉnh không phải hàng bán (Voucher, Dùng N điểm, Phí ship)
        non_item_pat = re.compile(
            r"^(nan|none|null|voucher|dùng\s+\d+\s*điểm|phí\s*ship|phí\s*giao\s*hàng|phí\s*vận\s*chuyển|giảm\s*giá\s*hđ|chiết\s*khấu\s*hđ)$",
            re.IGNORECASE,
        )
        uniq_items = item_df["item_name"].unique()
        valid_item_set = {
            x
            for x in uniq_items
            if bool(str(x).strip()) and not bool(non_item_pat.match(str(x).strip()))
        }
        item_df = item_df[item_df["item_name"].isin(valid_item_set)].copy()

        if c_qty_base:
            item_df["qty_base"] = clean_currency_series(
                df.loc[item_df.index, c_qty_base]
            )
        else:
            item_df["qty_base"] = 0.0

        item_df["unit_price"] = (
            clean_currency_series(df.loc[item_df.index, c_price])
            if c_price
            else 0.0
        )
        if c_amount:
            item_df["amount"] = clean_currency_series(
                df.loc[item_df.index, c_amount]
            )
        else:
            item_df["amount"] = item_df["qty"] * item_df["unit_price"]

        item_df["cost_price"] = (
            clean_currency_series(df.loc[item_df.index, c_cost])
            if c_cost
            else 0.0
        )
        item_df["_has_line_cost"] = bool(c_cost is not None)

        if not item_df.empty:
            pos_prices = item_df[item_df["unit_price"] > 0]["unit_price"]
            if not pos_prices.empty:
                med_price = pos_prices.median()
                if 0 < med_price < 500:
                    item_df["unit_price"] *= 1000.0
                    item_df["amount"] *= 1000.0
                    item_df["cost_price"] *= 1000.0

        if not order_df.empty:
            pos_tot = order_df[order_df["total_order"] > 0]["total_order"]
            if not pos_tot.empty:
                med_tot = pos_tot.median()
                if 0 < med_tot < 500:
                    order_df["total_order"] *= 1000.0
                    order_df["debt"] *= 1000.0
                    order_df["paid"] *= 1000.0

        return order_df, item_df

    def extract_imports(self, fname_l: str, df: pd.DataFrame) -> Optional[pd.DataFrame]:
        cols = list(df.columns)
        c_code = self.find_col(
            cols,
            [
                r"mã nhập hàng",
                r"mã đơn nhập",
                r"mã phiếu nhập",
                r"số phiếu",
                r"mã nhập",
                r"maphieunhap",
                r"ma_phieu_nhap",
                r"so_phieu",
            ],
        )
        c_date = self.find_col(
            cols,
            [r"ngày nhập", r"ngaynhap", r"ngay_nhap", r"thời gian", r"ngày", r"ngày chi"],
        )
        c_line_amt = self.find_col(
            cols, [r"thành tiền", r"thanh tien", r"thanh_tien", r"thanhtien"]
        )
        c_receipt_tot = self.find_col(
            cols,
            [
                r"tổng giá trị nhập",
                r"tonggiatrinhap",
                r"tổng tiền phiếu",
                r"cần trả ncc",
                r"tổng tiền",
                r"so tien",
                r"giá trị",
            ],
        )
        c_status = self.find_col(
            cols, [r"trạng thái phiếu", r"trạng thái", r"trangthai", r"trang_thai"]
        )
        c_supplier = self.find_col(
            cols,
            [r"mã nhà cung cấp", r"mã ncc", r"mancc", r"ma_ncc", r"nhà cung cấp", r"ncc"],
        )
        c_item_name = self.find_col(
            cols,
            [
                r"tên mặt hàng",
                r"tên hàng",
                r"tên sản phẩm",
                r"hàng hóa",
                r"^hàng$",
                r"product",
                r"mô tả",
                r"sản phẩm",
                r"ten_hang",
                r"ten_sp",
            ],
        )
        c_item_code = self.find_col(
            cols,
            [
                r"mã sku",
                r"mã hàng bán",
                r"mã vạch",
                r"mã hàng",
                r"mã sp",
                r"masp",
                r"ma_sp",
            ],
        )
        c_qty = self.find_col(
            cols,
            [r"số lượng nhập", r"sl nhập", r"sl_nhap", r"slnhap", r"số lượng", r"^sl$"],
        )
        c_price = self.find_col(
            cols,
            [
                r"đơn giá nhập",
                r"dongianhap",
                r"giá nhập",
                r"gianhap",
                r"đơn giá",
                r"don_gia",
            ],
        )
        c_unit = self.find_col(
            cols,
            [r"đvt nhập", r"dvt_nhap", r"đơn vị nhập", r"đvt", r"dvt", r"đơn vị"],
        )
        c_lot = self.find_col(cols, [r"số lô", r"mã lô", r"so_lo", r"solo", r"lô"])
        c_exp = self.find_col(
            cols, [r"hạn dùng", r"hạn sử dụng", r"hsd", r"han_dung", r"han_su_dung"]
        )

        if not c_line_amt and not c_receipt_tot and not c_price:
            return None

        imp_df = pd.DataFrame()
        if c_code:
            imp_df["import_id"] = df[c_code].fillna("").astype(str).str.strip()
        else:
            imp_df["import_id"] = [f"imp_{i}" for i in range(len(df))]

        fallback_yr = None
        if "_sheet_year" in df.columns:
            valid_yrs = df["_sheet_year"].dropna()
            if not valid_yrs.empty:
                fallback_yr = int(valid_yrs.iloc[0])
        if not fallback_yr and self.loader.max_data_date:
            fallback_yr = int(self.loader.max_data_date[:4])

        imp_df["date"] = (
            df[c_date].apply(lambda x: parse_date_str(x, default_year=fallback_yr))
            if c_date
            else None
        )
        imp_df["status"] = (
            df[c_status].fillna("").astype(str).str.strip().str.lower()
            if c_status
            else "hoàn thành"
        )
        imp_df["supplier"] = (
            df[c_supplier].fillna("").astype(str).str.strip() if c_supplier else ""
        )
        imp_df["item_name"] = (
            df[c_item_name].fillna("").astype(str).str.strip() if c_item_name else ""
        )
        imp_df["item_code"] = (
            df[c_item_code].fillna("").astype(str).str.strip()
            if c_item_code
            else imp_df["item_name"]
        )
        imp_df["unit"] = (
            df[c_unit].fillna("").astype(str).str.strip().str.lower()
            if c_unit
            else ""
        )
        imp_df["qty"] = df[c_qty].apply(clean_currency_str) if c_qty else 1.0
        imp_df["unit_price"] = (
            df[c_price].apply(clean_currency_str) if c_price else 0.0
        )
        imp_df["lot"] = df[c_lot].fillna("").astype(str).str.strip() if c_lot else ""
        imp_df["expiry_date"] = (
            df[c_exp].apply(lambda x: parse_date_str(x, default_year=fallback_yr))
            if c_exp
            else None
        )

        if c_line_amt:
            imp_df["total_amount"] = df[c_line_amt].apply(clean_currency_str)
            imp_df["_is_receipt_total"] = False
        elif c_price and c_qty:
            imp_df["total_amount"] = imp_df["qty"] * imp_df["unit_price"]
            imp_df["_is_receipt_total"] = False
        elif c_receipt_tot:
            imp_df["total_amount"] = df[c_receipt_tot].apply(clean_currency_str)
            imp_df["_is_receipt_total"] = True
        else:
            imp_df["total_amount"] = 0.0
            imp_df["_is_receipt_total"] = False

        return imp_df

    def extract_expenses(self, df: pd.DataFrame) -> Optional[pd.DataFrame]:
        cols = list(df.columns)
        c_date = self.find_col(cols, [r"ngày chi", r"ngày", r"thời gian", r"tháng"])
        c_cat = self.find_col(cols, [r"mục", r"khoản chi", r"loại chi phí", r"nội dung"])
        c_amt = self.find_col(cols, [r"tiền chi", r"so tien", r"số tiền", r"tiền"])

        if not c_amt:
            return None

        fallback_yr = None
        if "_sheet_year" in df.columns:
            valid_yrs = df["_sheet_year"].dropna()
            if not valid_yrs.empty:
                fallback_yr = int(valid_yrs.iloc[0])
        if not fallback_yr and self.loader.max_data_date:
            fallback_yr = int(self.loader.max_data_date[:4])

        exp_df = pd.DataFrame()
        exp_df["date"] = (
            df[c_date].apply(lambda x: parse_date_str(x, default_year=fallback_yr))
            if c_date
            else None
        )
        exp_df["category"] = (
            df[c_cat].fillna("").astype(str).str.strip() if c_cat else ""
        )
        exp_df["amount"] = df[c_amt].apply(clean_currency_str)
        return exp_df

    def link_orders_and_items(self) -> None:
        if (
            self.customers_df is not None
            and not self.customers_df.empty
            and self.orders_df is not None
            and not self.orders_df.empty
        ):
            cid_to_cname = dict(
                zip(self.customers_df["cust_id"], self.customers_df["cust_name"])
            )
            self.orders_df["cust_name"] = [
                cid_to_cname.get(str(cid).strip(), str(cn).strip())
                if not str(cn).strip() or str(cn).strip() in ("0", "nan", "None")
                else str(cn).strip()
                for cid, cn in zip(
                    self.orders_df["cust_id"], self.orders_df["cust_name"]
                )
            ]

        if self.items_df is not None and self.orders_df is not None:
            ords = self.orders_df.dropna(subset=["order_id"]).copy()

            def status_priority(s: Any) -> int:
                if not s or pd.isna(s):
                    return 0
                s_l = str(s).lower().strip()
                if s_l.isdigit() or s_l in ("none", "nan"):
                    return 0
                if (
                    any(
                        k in s_l
                        for k in [
                            "huỷ",
                            "hủy",
                            "cancel",
                            "không giao được",
                            "trả",
                            "tra hang",
                            "hoàn hàng",
                            "hoàn trả",
                            "return",
                        ]
                    )
                    or re.search(r"\bhuy\b", s_l)
                ):
                    return 3
                if any(
                    k in s_l
                    for k in [
                        "hoàn thành",
                        "completed",
                        "đã giao",
                        "thành công",
                        "đã lấy",
                        "vận chuyển",
                        "đang giao",
                        "shipping",
                    ]
                ):
                    return 2
                return 1

            ords["has_date"] = ords["date"].notna() & (ords["date"] != "")
            ords["priority"] = ords["status"].apply(status_priority)
            ords = ords.sort_values(
                by=["has_date", "priority"], ascending=[False, False]
            ).drop_duplicates("order_id")
            self.orders_df = ords.drop(columns=["has_date", "priority"]).copy()

            date_map = dict(zip(ords["order_id"], ords["date"]))
            raw_date_map = dict(zip(ords["order_id"], ords["raw_date"]))
            status_map = dict(zip(ords["order_id"], ords["status"]))
            cust_id_map = dict(zip(ords["order_id"], ords["cust_id"]))
            cust_name_map = dict(zip(ords["order_id"], ords["cust_name"]))
            pay_map = dict(zip(ords["order_id"], ords["payment_method"]))
            chan_map = dict(zip(ords["order_id"], ords["channel"]))
            staff_map = dict(zip(ords["order_id"], ords["staff"]))

            self.items_df = self.items_df.copy()
            mapped_date = self.items_df["order_id"].map(date_map)
            if "date" in self.items_df:
                self.items_df["date"] = (
                    self.items_df["date"].replace("", pd.NA).fillna(mapped_date)
                )
            else:
                self.items_df["date"] = mapped_date
            self.items_df["date"] = self.items_df["date"].fillna("").astype(str)

            mapped_raw = self.items_df["order_id"].map(raw_date_map)
            if "raw_date" in self.items_df:
                self.items_df["raw_date"] = (
                    self.items_df["raw_date"].replace("", pd.NA).fillna(mapped_raw)
                )
            else:
                self.items_df["raw_date"] = mapped_raw

            mapped_status = self.items_df["order_id"].map(status_map)
            if "status" in self.items_df:
                self.items_df["status"] = (
                    self.items_df["status"]
                    .replace({"None": pd.NA, "nan": pd.NA, "": pd.NA})
                    .fillna(mapped_status)
                )
            else:
                self.items_df["status"] = mapped_status

            self.items_df["status"] = self.items_df["status"].fillna("hoàn thành")

            if self.returned_order_ids:
                ret_mask = self.items_df["order_id"].isin(self.returned_order_ids)
                self.items_df.loc[ret_mask, "status"] = "trả hàng"
                if self.orders_df is not None:
                    ord_ret_mask = self.orders_df["order_id"].isin(
                        self.returned_order_ids
                    )
                    self.orders_df.loc[ord_ret_mask, "status"] = "trả hàng"

            for col_k, mp in [
                ("cust_id", cust_id_map),
                ("cust_name", cust_name_map),
                ("payment_method", pay_map),
                ("channel", chan_map),
                ("staff", staff_map),
            ]:
                if col_k not in self.items_df or (self.items_df[col_k] == "").all():
                    self.items_df[col_k] = (
                        self.items_df["order_id"].map(mp).fillna("")
                    )

        # Ánh xạ mã hàng <-> tên hàng và nhóm hàng từ danh mục
        if (
            self.items_df is not None
            and not self.items_df.empty
            and self.products_df is not None
            and not self.products_df.empty
        ):
            code_to_name = dict(
                zip(self.products_df["item_code"], self.products_df["item_name"])
            )
            code_to_group = dict(
                zip(self.products_df["item_code"], self.products_df["item_group"])
            )
            name_to_code = dict(
                zip(
                    self.products_df["item_name"].str.lower().str.strip(),
                    self.products_df["item_code"],
                )
            )
            name_to_group = dict(
                zip(
                    self.products_df["item_name"].str.lower().str.strip(),
                    self.products_df["item_group"],
                )
            )

            self.items_df["item_code"] = [
                name_to_code.get(str(n).lower().strip(), c) if c == n else c
                for c, n in zip(
                    self.items_df["item_code"], self.items_df["item_name"]
                )
            ]
            self.items_df["item_name"] = [
                code_to_name.get(c, n)
                for c, n in zip(
                    self.items_df["item_code"], self.items_df["item_name"]
                )
            ]
            self.items_df["item_group"] = [
                code_to_group.get(c)
                or name_to_group.get(str(n).lower().strip(), "")
                for c, n in zip(
                    self.items_df["item_code"], self.items_df["item_name"]
                )
            ]

        # Chuẩn hóa gộp các mặt hàng trùng tên khác hoa/thường khi bảng bán không có mã SKU riêng
        if self.items_df is not None and not self.items_df.empty:
            canon_name_map: Dict[str, str] = {}
            canon_code_map: Dict[str, str] = {}
            for c, n in zip(self.items_df["item_code"], self.items_df["item_name"]):
                nl = str(n).lower().strip()
                if not nl:
                    continue
                if nl not in canon_name_map or (
                    canon_name_map[nl].isupper() and not str(n).isupper()
                ):
                    canon_name_map[nl] = str(n).strip()
                if nl not in canon_code_map or canon_code_map[nl] == canon_name_map.get(nl, ""):
                    if str(c).strip() != str(n).strip():
                        canon_code_map[nl] = str(c).strip()
                    elif nl not in canon_code_map:
                        canon_code_map[nl] = str(n).strip()

            self.items_df["item_name"] = [
                canon_name_map.get(str(n).lower().strip(), str(n).strip())
                for n in self.items_df["item_name"]
            ]
            self.items_df["item_code"] = [
                canon_code_map.get(str(n).lower().strip(), str(c).strip())
                if str(c).strip() == str(n).strip() or str(c).lower().strip() == str(n).lower().strip()
                else str(c).strip()
                for c, n in zip(self.items_df["item_code"], self.items_df["item_name"])
            ]

        if (
            self.imports_df is not None
            and not self.imports_df.empty
            and self.products_df is not None
            and not self.products_df.empty
        ):
            code_to_name = dict(
                zip(self.products_df["item_code"], self.products_df["item_name"])
            )
            self.imports_df["item_name"] = [
                code_to_name.get(c, n) if (not n or n == c or n == "nan") else n
                for c, n in zip(
                    self.imports_df["item_code"], self.imports_df["item_name"]
                )
            ]

        # Tính số lượng quy đổi về đơn vị cơ bản (qty_base) bằng vòng lặp zip tốc độ cao
        if self.items_df is not None and not self.items_df.empty:
            conv_lower = {
                k.lower().strip(): v for k, v in self.unit_conversions.items()
            }
            if not conv_lower:
                qb_s = self.items_df["qty_base"]
                q_s = self.items_df["qty"]
                self.items_df["qty_base"] = np.where(qb_s > 0, qb_s, q_s)
            else:
                new_qb: List[float] = []
                for qb, q, unit_v, iname_v, icode_v in zip(
                    self.items_df["qty_base"],
                    self.items_df["qty"],
                    self.items_df["unit"],
                    self.items_df["item_name"],
                    self.items_df["item_code"],
                ):
                    if qb and qb > 0 and qb != q:
                        new_qb.append(float(qb))
                        continue
                    unit = str(unit_v).lower().strip()
                    if not unit:
                        new_qb.append(float(q if q > 0 else qb))
                        continue
                    iname = str(iname_v).lower().strip()
                    icode = str(icode_v).lower().strip()
                    u_map = conv_lower.get(iname) or conv_lower.get(icode)
                    if u_map:
                        if unit in u_map:
                            new_qb.append(float(q * u_map[unit]))
                            continue
                        matched_rate = None
                        for uk, urate in u_map.items():
                            if uk.startswith(unit + " ") or unit.startswith(uk + " "):
                                matched_rate = urate
                                break
                        if matched_rate is not None:
                            new_qb.append(float(q * matched_rate))
                            continue
                    new_qb.append(float(qb if qb > 0 else q))
                self.items_df["qty_base"] = new_qb

    def enrich_views(self) -> None:
        """
        Chuẩn hóa thêm các cột hợp đồng V['sales'] và V['orders'] theo Blueprint §3.4.2:
        - status_norm: 'hoan_thanh', 'huy', 'tra_hang', 'dang_giao'
        - is_valid: status_norm not in ('huy', 'tra_hang') (cấp độ cả đơn)
        - is_service_fee: dòng 'Phí phục vụ'
        - hour: giờ tạo đơn (0..23)
        - cust_key: mã khách (nếu shop có mã) hoặc tên khách chuẩn hóa; khách lẻ = ''
        """
        if self.items_df is not None and not self.items_df.empty:
            df = self.items_df
            uniq_st = {s: normalize_status_4(s) for s in df["status"].unique()}
            df["status_norm"] = df["status"].map(uniq_st)
            bad_orders = set(
                df[df["status_norm"].isin(["huy", "tra_hang"])]["order_id"]
                .dropna()
                .unique()
            )
            df["is_valid"] = ~df["order_id"].isin(bad_orders)
            df["is_service_fee"] = (
                df["item_name"].str.lower().str.contains("phí phục vụ", na=False)
            )

            def extract_hour(raw_d: Any) -> Optional[int]:
                m = re.search(r"(\d{1,2}):(\d{2})", str(raw_d))
                return int(m.group(1)) if m else None

            uniq_rd = {rd: extract_hour(rd) for rd in df["raw_date"].unique()}
            df["hour"] = df["raw_date"].map(uniq_rd)
            df["line_cost"] = df["qty"] * df["cost_price"]

            uniq_cids = df["cust_id"].unique() if "cust_id" in df.columns else []
            has_code = any(
                bool(str(x).strip())
                and str(x).strip().lower() not in ("nan", "none", "khách lẻ", "khach le")
                for x in uniq_cids
            )

            walkin_set = {
                "",
                "nan",
                "none",
                "khách lẻ",
                "khach le",
                "khách vãng lai",
                "vãng lai",
                "vang lai",
                "kl",
                "k.lẻ",
                "k.le",
                "lẻ",
                "le",
                "-",
                "0",
            }
            cust_keys: List[str] = []
            for cid_v, cname_v in zip(df["cust_id"], df["cust_name"]):
                cid = str(cid_v).strip()
                cname = str(cname_v).strip()
                if cname.lower() in walkin_set or cid.lower() in {
                    "khách lẻ",
                    "khach le",
                    "kl",
                    "k.lẻ",
                    "k.le",
                    "lẻ",
                    "le",
                    "vãng lai",
                    "vang lai",
                }:
                    cust_keys.append("")
                elif has_code and cid and cid.lower() not in walkin_set:
                    cust_keys.append(cid)
                elif cname.lower() not in walkin_set:
                    cust_keys.append(re.sub(r"\s+", " ", cname.lower()).strip())
                else:
                    cust_keys.append("")

            df["cust_key"] = cust_keys
            self.items_df = df

        if self.orders_df is not None and not self.orders_df.empty:
            odf = self.orders_df
            uniq_ost = {s: normalize_status_4(s) for s in odf["status"].unique()}
            odf["status_norm"] = odf["status"].map(uniq_ost)
            odf["is_valid"] = ~odf["status_norm"].isin(["huy", "tra_hang"])


@dataclass
class ShopProfile:
    business_id: str
    readme: str
    readme_max_date: Optional[str]
    min_date: Optional[str]
    max_date: Optional[str]
    capabilities: Dict[str, bool] = field(default_factory=dict)
    empty_columns: List[str] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    unit_conversions: Dict[str, Dict[str, float]] = field(default_factory=dict)


@dataclass
class ShopBundle:
    business_id: str
    loader: DataLoader
    adapter: BusinessAdapter
    profile: ShopProfile
    dfs: Dict[str, pd.DataFrame]
    views: Dict[str, pd.DataFrame]
    schema_card: str
    entity_index: Dict[str, Any] = field(default_factory=dict)


class SchemaBuilder:
    """
    Module Xây dựng Schema & Hồ sơ năng lực Cửa hàng (schema_builder.py):
    - Nạp và cache ShopBundle theo business_id.
    - Trích xuất ShopProfile (capabilities, khoảng ngày dữ liệu [min_date, max_date]) cho Unanswerable Gate.
    - Sinh Schema Card (chuẩn BIRD: tên bảng, tên cột, kiểu dữ liệu, giá trị mẫu, tóm tắt quy ước README) cho Qwen.
    """

    def __init__(self, data_root: str) -> None:
        self.data_root = data_root
        self._cache: Dict[str, ShopBundle] = {}

    def get_bundle(self, business_id: str) -> Optional[ShopBundle]:
        if business_id in self._cache:
            return self._cache[business_id]

        biz_path = os.path.join(self.data_root, "data", business_id)
        if not os.path.exists(biz_path):
            return None

        loader = DataLoader(biz_path)
        adapter = BusinessAdapter(loader)
        views = self.build_views(loader, adapter)
        profile = self.build_profile(business_id, loader, adapter, views)
        schema_card = self.render_schema_card(profile, views, loader.data_files)

        bundle = ShopBundle(
            business_id=business_id,
            loader=loader,
            adapter=adapter,
            profile=profile,
            dfs=loader.data_files,
            views=views,
            schema_card=schema_card,
        )
        self._cache[business_id] = bundle
        return bundle

    def build_views(
        self, loader: DataLoader, adapter: BusinessAdapter
    ) -> Dict[str, pd.DataFrame]:
        views: Dict[str, pd.DataFrame] = {}
        if adapter.items_df is not None and not adapter.items_df.empty:
            views["sales"] = adapter.items_df
        if adapter.orders_df is not None and not adapter.orders_df.empty:
            views["orders"] = adapter.orders_df
        if adapter.products_df is not None and not adapter.products_df.empty:
            views["products"] = adapter.products_df
        if adapter.imports_df is not None and not adapter.imports_df.empty:
            views["imports"] = adapter.imports_df
        if adapter.expenses_df is not None and not adapter.expenses_df.empty:
            views["expenses"] = adapter.expenses_df
        if adapter.inventory_df is not None and not adapter.inventory_df.empty:
            views["inventory"] = adapter.inventory_df
        if adapter.customers_df is not None and not adapter.customers_df.empty:
            views["customers"] = adapter.customers_df
        return views

    def build_profile(
        self,
        business_id: str,
        loader: DataLoader,
        adapter: BusinessAdapter,
        views: Dict[str, pd.DataFrame],
    ) -> ShopProfile:
        min_date: Optional[str] = None
        max_date: Optional[str] = loader.max_data_date

        for df_check in [adapter.items_df, adapter.orders_df]:
            if (
                df_check is not None
                and not df_check.empty
                and "date" in df_check.columns
            ):
                valid_dates = [
                    d
                    for d in df_check["date"].dropna().unique()
                    if isinstance(d, str) and len(d) == 10 and d.startswith("202")
                ]
                if valid_dates:
                    d_min = min(valid_dates)
                    d_max = max(valid_dates)
                    if min_date is None or d_min < min_date:
                        min_date = d_min
                    if max_date is None:
                        max_date = d_max
                    break

        has_items = (
            adapter.items_df is not None
            and not adapter.items_df.empty
            and not adapter.is_order_summary_only
            and not loader.is_no_item_detail_shop
        )
        has_orders = (
            adapter.orders_df is not None and not adapter.orders_df.empty
        ) or (adapter.items_df is not None and not adapter.items_df.empty)
        has_imports = (
            adapter.imports_df is not None and not adapter.imports_df.empty
        )
        has_expenses = (
            adapter.expenses_df is not None and not adapter.expenses_df.empty
        )
        has_inventory = (
            adapter.inventory_df is not None and not adapter.inventory_df.empty
        ) or any(
            any(k in f.lower() for k in ["ton", "kho", "lo_hang", "lohang"])
            for f in loader.data_files.keys()
        )
        has_rx = (
            adapter.products_df is not None
            and "is_prescription" in adapter.products_df.columns
            and bool(adapter.products_df["is_prescription"].any())
        )
        has_active = (
            adapter.products_df is not None
            and "active_ingredient" in adapter.products_df.columns
            and bool((adapter.products_df["active_ingredient"] != "").any())
        )
        has_line_cost = (
            adapter.items_df is not None
            and "_has_line_cost" in adapter.items_df.columns
            and bool(adapter.items_df["_has_line_cost"].any())
            and bool((adapter.items_df["cost_price"] > 0).any())
        )
        has_catalog_cost = (
            adapter.products_df is not None
            and "cost_price" in adapter.products_df.columns
            and bool((adapter.products_df["cost_price"] > 0).any())
        )
        # Kiểm tra các cột có dữ liệu thật trong toàn bộ bảng của shop (trừ cột rỗng đã ghi trong README)
        all_cols_non_empty: Set[str] = set()
        for _, raw_df in loader.data_files.items():
            for c in raw_df.columns:
                cl = c.lower().strip()
                if cl in loader.readme_empty_cols:
                    continue
                non_na = raw_df[c].dropna()
                non_empty = [
                    str(v).strip()
                    for v in non_na
                    if str(v).strip() and str(v).strip().lower() not in ("nan", "none")
                ]
                if non_empty:
                    all_cols_non_empty.add(cl)

        has_lot_expiry = (
            (
                adapter.imports_df is not None
                and "expiry_date" in adapter.imports_df.columns
                and bool(adapter.imports_df["expiry_date"].notna().any())
            )
            or any(
                "lo" in f.lower() or "han_dung" in f.lower()
                for f in loader.data_files.keys()
            )
            or any(
                any(k in c for k in ["hạn sử dụng", "han_su_dung", "hạn dùng", "han_dung"])
                for c in all_cols_non_empty
            )
        )

        # Kiểm tra cột nợ / phiếu thu / sổ nợ
        has_debt = (
            (
                adapter.orders_df is not None
                and "debt" in adapter.orders_df.columns
                and bool((adapter.orders_df["debt"] > 0).any())
            )
            or any(
                any(
                    k in f.lower()
                    for k in [
                        "no",
                        "phieuthu",
                        "phieu_thu",
                        "thanhtoan",
                        "thanh_toan",
                        "soquy",
                        "so_quy",
                        "gop",
                    ]
                )
                for f in loader.data_files.keys()
            )
            or any(
                any(
                    k in c
                    for k in [
                        "còn nợ",
                        "con_no",
                        "dư nợ",
                        "chưa trả",
                        "khách đã trả",
                        "hạn thanh toán",
                        "số ngày được nợ",
                        "còn lại",
                    ]
                )
                for c in all_cols_non_empty
            )
        )

        has_customer = not loader.is_no_customer_shop and (
            (
                adapter.items_df is not None
                and "cust_key" in adapter.items_df.columns
                and bool((adapter.items_df["cust_key"] != "").any())
            )
            or (
                adapter.customers_df is not None
                and not adapter.customers_df.empty
            )
            or any(
                any(
                    k in c
                    for k in [
                        "mã kh",
                        "ma_kh",
                        "makh",
                        "số kh",
                        "khách hàng",
                        "ten_kh",
                        "tên kh",
                    ]
                )
                for c in all_cols_non_empty
            )
        )
        has_staff = (
            (
                adapter.items_df is not None
                and "staff" in adapter.items_df.columns
                and bool((adapter.items_df["staff"] != "").any())
            )
            or any(
                any(k in f.lower() for k in ["nhanvien", "nhan_vien", "tho"])
                for f in loader.data_files.keys()
            )
            or any(
                any(
                    k in c
                    for k in [
                        "nhân viên",
                        "nhan_vien",
                        "người bán",
                        "nguoi_ban",
                        "thợ",
                        "thu ngân",
                    ]
                )
                for c in all_cols_non_empty
            )
        )
        has_payment_method = (
            (
                adapter.items_df is not None
                and "payment_method" in adapter.items_df.columns
                and bool((adapter.items_df["payment_method"] != "").any())
            )
            or any(
                "thanhtoan" in f.lower() or "thanh_toan" in f.lower()
                for f in loader.data_files.keys()
            )
            or any(
                any(
                    k in c
                    for k in [
                        "phương thức",
                        "phuong_thuc",
                        "hình thức tt",
                        "hinh_thuc_tt",
                        "thanh toán bằng",
                        "trả bằng",
                        "pttt",
                        "httt",
                    ]
                )
                for c in all_cols_non_empty
            )
        )
        has_channel = (
            adapter.items_df is not None
            and "channel" in adapter.items_df.columns
            and bool((adapter.items_df["channel"] != "").any())
        ) or any(
            any(
                k in c
                for k in ["kênh", "kenh", "bán qua", "ban_qua", "loại bán", "loai_ban", "nguồn đơn", "sàn", "phòng/bàn"]
            )
            for c in all_cols_non_empty
        )
        has_address = any(
            any(k in c for k in ["địa chỉ", "dia_chi", "addr", "đường", "quận", "phường"])
            for c in all_cols_non_empty
        )
        has_gender = any(
            any(k in c for k in ["giới tính", "gioi_tinh", "phái"])
            for c in all_cols_non_empty
        )
        has_cust_group = any(
            any(
                k in c
                for k in [
                    "nhóm khách",
                    "nhom_kh",
                    "hạng thẻ",
                    "loại khách",
                    "loại hình khách",
                    "khách lẻ/sỉ",
                ]
            )
            or c == "nhóm"
            for c in all_cols_non_empty
        )
        has_reason = any(
            any(k in c for k in ["lý do", "ly_do", "nguyên nhân"])
            for c in all_cols_non_empty
        )

        capabilities = {
            "has_items": has_items,
            "has_orders": has_orders,
            "has_imports": has_imports,
            "has_expenses": has_expenses,
            "has_inventory": has_inventory,
            "has_rx": has_rx,
            "has_active": has_active,
            "has_line_cost": has_line_cost,
            "has_catalog_cost": has_catalog_cost,
            "has_lot_expiry": has_lot_expiry,
            "has_debt": has_debt,
            "has_customer": has_customer,
            "has_staff": has_staff,
            "has_payment_method": has_payment_method,
            "has_channel": has_channel,
            "has_address": has_address,
            "has_gender": has_gender,
            "has_cust_group": has_cust_group,
            "has_reason": has_reason,
        }

        return ShopProfile(
            business_id=business_id,
            readme=loader.readme_content,
            readme_max_date=loader.max_data_date,
            min_date=min_date,
            max_date=max_date,
            capabilities=capabilities,
            empty_columns=sorted(loader.readme_empty_cols),
            files=sorted(loader.data_files.keys()),
            unit_conversions=adapter.unit_conversions,
        )

    def render_schema_card(
        self,
        profile: ShopProfile,
        views: Dict[str, pd.DataFrame],
        dfs: Dict[str, pd.DataFrame],
    ) -> str:
        """
        Sinh biểu diễn Schema cô đọng, kèm giá trị mẫu (BIRD-style Value-Aware Schema)
        để nạp vào Prompt cho Qwen2.5-Coder-14B-Instruct.
        """
        lines: List[str] = []
        lines.append(
            f"Cửa hàng: {profile.business_id} | Khoảng ngày dữ liệu: {profile.min_date} -> {profile.max_date}"
        )
        lines.append("## 1. BẢNG CHUẨN HÓA SẴN TRONG `V` (Ưu tiên dùng):")
        for vname, vdf in views.items():
            col_summaries = []
            for col in vdf.columns:
                if col.startswith("_"):
                    continue
                samples = [
                    str(x)
                    for x in vdf[col].dropna().unique()[:3]
                    if str(x).strip() != ""
                ]
                samp_str = ", ".join(repr(s[:35]) for s in samples[:2])
                col_summaries.append(f"{col} (mẫu: {samp_str})")
            lines.append(f"- V['{vname}'] ({len(vdf)} dòng): " + "; ".join(col_summaries[:12]))

        lines.append("\n## 2. BẢNG GỐC TRONG `dfs`:")
        for fname, fdf in dfs.items():
            cols = [c for c in fdf.columns if not c.startswith("_")]
            lines.append(f"- dfs['{fname}'] ({len(fdf)} dòng): cột = {cols[:12]}")

        if profile.empty_columns:
            lines.append(
                f"\n## 3. CẢNH BÁO CỘT TRỐNG (không có dữ liệu): {profile.empty_columns}"
            )
        return "\n".join(lines)

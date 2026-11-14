import os
import re
import sqlite3
import pandas as pd
try:
    pd.options.future.infer_string = False
except Exception:
    pass
from datetime import datetime

def clean_currency_str(val):
    if pd.isna(val):
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().lower()
    if not s:
        return 0.0
    
    # Handle "k" suffix (e.g. 25000k -> 25,000,000; 198k -> 198,000)
    multiplier = 1.0
    if re.search(r'\d\s*k$', s):
        multiplier = 1000.0
        s = re.sub(r'k$', '', s).strip()
    elif re.search(r'\d\s*(tr|triệu)$', s):
        multiplier = 1000000.0
        s = re.sub(r'(tr|triệu)$', '', s).strip()
    
    # Remove currency symbol 'đ' or 'vnd'
    s = s.replace('đ', '').replace('vnd', '').strip()

    # If quantity or number has trailing unit word (e.g. "1 hộp", "2 cục", "5 gói" in biz_103)
    m_num_unit = re.match(r'^([-+]?\d+(?:[.,]\d+)*)\s+[^\d]+$', s)
    if m_num_unit:
        s = m_num_unit.group(1).strip()
    
    # Handle dots and commas (Vietnamese: dot is thousand separator, comma is decimal)
    if '.' in s and ',' in s:
        s = s.replace('.', '').replace(',', '.')
    elif '.' in s:
        parts = s.split('.')
        if len(parts) > 1 and all(len(p) == 3 for p in parts[1:]):
            s = s.replace('.', '')
        elif len(parts) == 2 and len(parts[1]) == 3:
            s = s.replace('.', '')
    elif ',' in s:
        parts = s.split(',')
        if len(parts) > 1 and all(len(p) == 3 for p in parts[1:]):
            s = s.replace(',', '')
        else:
            s = s.replace(',', '.')

    try:
        return float(s) * multiplier
    except Exception:
        return 0.0

def parse_date_str(val, default_year=None):
    if pd.isna(val):
        return None
    if isinstance(val, (datetime, pd.Timestamp)):
        return val.strftime('%Y-%m-%d')
    if isinstance(val, (int, float)):
        try:
            if 30000 <= float(val) <= 60000:
                dt = pd.to_datetime(float(val), unit='D', origin='1899-12-30')
                return dt.strftime('%Y-%m-%d')
        except Exception:
            pass
    s = str(val).strip()
    if not s:
        return None
    try:
        f_val = float(s)
        if 30000 <= f_val <= 60000:
            dt = pd.to_datetime(f_val, unit='D', origin='1899-12-30')
            return dt.strftime('%Y-%m-%d')
    except Exception:
        pass

    # Check Vietnamese format: "25 thg 5 2026", "25 thg 05, 2026", "25 tháng 5, 2026"
    m_thg = re.search(r'(\d{1,2})\s+(?:thg|tháng)\s+(\d{1,2})(?:[,\s]+(\d{4}))?', s, re.IGNORECASE)
    if m_thg:
        d = int(m_thg.group(1))
        m = int(m_thg.group(2))
        y = int(m_thg.group(3)) if m_thg.group(3) else default_year
        if y:
            return f"{y:04d}-{m:02d}-{d:02d}"

    # Strip time if present
    s_date = s.split()[0].split('T')[0]
    
    # Try common formats
    for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y', '%Y/%m/%d', '%m/%d/%Y'):
        try:
            dt = datetime.strptime(s_date, fmt)
            return dt.strftime('%Y-%m-%d')
        except ValueError:
            pass

    # Day / month only: "1/10", "15/10", "01-10"
    m_dm = re.match(r'^(\d{1,2})[/-](\d{1,2})$', s_date)
    if m_dm and default_year:
        d = int(m_dm.group(1))
        m = int(m_dm.group(2))
        return f"{default_year:04d}-{m:02d}-{d:02d}"

    return None

class DataLoader:
    def __init__(self, biz_path):
        self.biz_path = biz_path
        self.data_files = {}
        self.unit_conversions = {} # product -> {unit: ratio}
        self.max_data_date = None
        self.metadata = {}
        self.load_all()

    def load_all(self):
        readme_path = os.path.join(self.biz_path, 'README.md')
        if os.path.exists(readme_path):
            self.parse_readme(readme_path)

        for fname in sorted(os.listdir(self.biz_path)):
            fpath = os.path.join(self.biz_path, fname)
            if fname == 'README.md' or fname.startswith('~$'):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext == '.csv':
                self.load_csv(fname, fpath)
            elif ext == '.xlsx':
                self.load_excel(fname, fpath)
            elif ext == '.sqlite':
                self.load_sqlite(fname, fpath)

    def parse_readme(self, readme_path):
        with open(readme_path, mode='r', encoding='utf-8', errors='ignore') as f:
            content = f.read()

        # Check max date: "Dữ liệu có đến hết 12/06/2026"
        m_date = re.search(r'hết\s+(\d{1,2}[/-]\d{1,2}[/-]\d{4})', content)
        if m_date:
            self.max_data_date = parse_date_str(m_date.group(1))

        # Check unit conversions in README:
        # "- Panadol Extra hộp 15 vỉ x 12 viên: 1 vỉ = 12 viên"
        # "- Gạo ST25: 1 bao 25kg = 25 kg"
        for line in content.split('\n'):
            line = line.strip()
            m_conv = re.search(r'-\s*(.+?):\s*1\s+([^=]+?)\s*=\s*(\d+)\s+([^\s,;.]+)', line)
            if m_conv:
                item_name = m_conv.group(1).strip()
                unit_large = m_conv.group(2).strip().lower()
                ratio = float(m_conv.group(3))
                if item_name not in self.unit_conversions:
                    self.unit_conversions[item_name] = {}
                self.unit_conversions[item_name][unit_large] = ratio

        # Also check inline rule like in dev_004:
        # "Ống PVC Bình Minh D32 cây 4m, Ống PVC Bình Minh D50 cây 4m, Ống PVC Bình Minh D90 cây 4m bán theo bó, 1 bó = 10 cây"
        m_inline = re.search(r'Hàng theo bó:\s*(.+?)\s*bán theo\s+([^\s,]+),\s*1\s+([^\s=]+)\s*=\s*(\d+)', content)
        if m_inline:
            items_part = m_inline.group(1)
            unit_l = m_inline.group(3).strip().lower()
            ratio = float(m_inline.group(4))
            for it in items_part.split(','):
                it_name = it.strip()
                if it_name:
                    if it_name not in self.unit_conversions:
                        self.unit_conversions[it_name] = {}
                    self.unit_conversions[it_name][unit_l] = ratio

    def find_header_row(self, df_raw):
        best_row = 0
        max_headers = 0
        for r in range(min(10, len(df_raw))):
            row_vals = df_raw.iloc[r].dropna().tolist()
            # Count string cells that look like column names (not dates or numbers)
            str_count = 0
            for v in row_vals:
                if isinstance(v, str):
                    vs = v.strip()
                    if len(vs) >= 2 and not re.match(r'^\d+[/-]\d+', vs) and not re.match(r'^\d+([.,]\d+)?$', vs):
                        str_count += 1
            if str_count > max_headers and str_count >= 3:
                max_headers = str_count
                best_row = r
        return best_row

    def load_excel(self, fname, fpath):
        try:
            xl = pd.ExcelFile(fpath)
            sheets_data = []
            default_yr = int(self.max_data_date[:4]) if self.max_data_date else None
            is_monthly = len(xl.sheet_names) > 1 and all(
                re.search(r'(?:tháng|thg|t\s*\d|\d{1,2}[._/-]202\d)', s, re.IGNORECASE) for s in xl.sheet_names
            )

            for sheet in xl.sheet_names:
                df_raw = xl.parse(sheet, header=None)
                if df_raw.empty:
                    continue
                header_idx = self.find_header_row(df_raw)
                header = [str(col).strip() if pd.notna(col) else f"col_{i}" for i, col in enumerate(df_raw.iloc[header_idx])]
                df = df_raw.iloc[header_idx + 1:].copy()
                df.columns = header
                df = df.dropna(how='all')
                if not df.empty:
                    m_yr = re.search(r'202\d', sheet)
                    if not m_yr:
                        m_yr = re.search(r'202\d', fname)
                    sheet_yr = int(m_yr.group(0)) if m_yr else default_yr
                    if sheet_yr:
                        df['_sheet_year'] = sheet_yr
                    df['_sheet_name'] = str(sheet).strip()
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
        except Exception as e:
            print(f"Error loading {fpath}: {e}")

    def load_csv(self, fname, fpath):
        try:
            df = pd.read_csv(fpath)
            df = self.normalize_table(df)
            self.data_files[fname] = df
        except Exception as e:
            print(f"Error loading {fpath}: {e}")

    def load_sqlite(self, fname, fpath):
        try:
            conn = sqlite3.connect(fpath)
            tables = [t[0] for t in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            for tbl in tables:
                df = pd.read_sql_query(f"SELECT * FROM {tbl}", conn)
                df = self.normalize_table(df)
                self.data_files[f"{fname}:{tbl}"] = df
            conn.close()
        except Exception as e:
            print(f"Error loading sqlite {fpath}: {e}")

    def normalize_table(self, df):
        # Strip column names
        df.columns = [str(c).strip() for c in df.columns]

        # 1. CRITICAL: Remove subtotal / summary rows ("Tổng ngày...", "Tổng tháng...", "Tổng cộng", "Total")
        # BEFORE forward-filling order_id/date so summary rows never inherit order_id/date!
        summary_pat = re.compile(
            r'^(tổng(\s+(ngày|tháng|quý|năm|cộng|kết|tiền|số|hđ|đơn|bill|phiếu))?|cộng(\s+(ngày|tháng|quý|năm))?|total)(\s|$|:)',
            re.IGNORECASE
        )
        str_cols = [c for c in df.columns if not c.startswith('_')]
        if str_cols:
            def is_summary_row(row):
                for c in str_cols:
                    val = row[c]
                    if pd.notna(val) and isinstance(val, str):
                        vs = val.strip()
                        if summary_pat.match(vs):
                            return True
                return False
            mask = df.apply(is_summary_row, axis=1)
            if mask.any():
                df = df[~mask].copy()

        # 2. Rulebook §8 (§142): Forward fill group/merged order-level cells
        ffill_keywords = [
            'mã đơn', 'ma don', 'mã hóa đơn', 'mã hđ', 'số hđ', 'mã bill', 'mã phiếu', 'số phiếu', 'số ct',
            'ngày đặt', 'ngày bán', 'ngày lập', 'ngày giao dịch', 'ngày tháng', 'ngày xuất',
            'trạng thái', 'tình trạng', 'kết quả', 'tiến độ', 'xử lý', 'status', 'tt đơn',
            'khách hàng', 'tenkh', 'tên kh', 'họ tên khách', 'đối tác'
        ]
        exact_ffill = {'hđ', 'hd', 'bill', 'ngày', 'ngay', 'date', 'thời gian', 'khách', 'kh'}
        for c in df.columns:
            c_l = c.lower().strip()
            if c_l in exact_ffill or any(k in c_l for k in ffill_keywords):
                if df[c].isna().any() and df[c].notna().any():
                    df[c] = df[c].ffill()

        return df

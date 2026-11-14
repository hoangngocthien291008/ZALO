import re
import pandas as pd
try:
    pd.options.future.infer_string = False
except Exception:
    pass
from datamate.data_loader import clean_currency_str, parse_date_str

class BusinessAdapter:
    def __init__(self, data_loader):
        self.loader = data_loader
        self.orders_df = None
        self.items_df = None
        self.products_df = None
        self.customers_df = None
        self.imports_df = None
        self.expenses_df = None
        self.returned_order_ids = set()
        self.abbrev_map = {}  # Ghi tắt -> Tên đầy đủ (dev_011, dev_014)
        self.is_order_summary_only = False
        self.unit_conversions = data_loader.unit_conversions.copy()
        self.normalize()

    def find_col(self, cols, patterns):
        for pat in patterns:
            for col in cols:
                if re.fullmatch(pat, col.strip().lower()):
                    return col
        for pat in patterns:
            for col in cols:
                if re.search(pat, col.strip().lower()):
                    return col
        return None

    def normalize(self):
        # 0. Extract abbreviation sheet ("Ghi tắt" in dev_011, dev_014)
        for fname, df in self.loader.data_files.items():
            fname_l = fname.lower()
            if 'ghi tắt' in fname_l or 'ghitat' in fname_l or 'ghi_tat' in fname_l:
                self.extract_abbreviations(df)

        # 1. Look for conversion tables in loaded files
        for fname, df in self.loader.data_files.items():
            fname_l = fname.lower()
            if any(k in fname_l for k in ['quydoi', 'donviquydoi', 'don_vi_sp', 'danhmuc', 'danh_muc']):
                self.extract_conversions(df)

        # 2. Extract products catalogue
        for fname, df in self.loader.data_files.items():
            fname_l = fname.lower()
            if any(k in fname_l for k in ['sanpham', 'san_pham', 'hanghoa', 'hang_hoa', 'danhmuc', 'danh_muc', 'danh_sach_hang_hoa']):
                self.extract_products(df)

        # 3. Extract medicine info if available
        for fname, df in self.loader.data_files.items():
            fname_l = fname.lower()
            if 'thuoc' in fname_l:
                self.extract_medicine_info(df)

        # 3b. Extract returned orders from dedicated return/lot tables (dev_005 DonHang_Lo, dev_006 tra_hang/chi_tiet_don_lo)
        for fname, df in self.loader.data_files.items():
            fname_l = fname.lower()
            if any(k in fname_l for k in ['donhang_lo', 'chi_tiet_don_lo', 'tra_hang', 'trahang']):
                self.extract_returned_orders(fname_l, df)

        # 4. Extract order items / sales, imports, expenses
        items_dfs = []
        orders_dfs = []
        imports_dfs = []
        expenses_dfs = []

        # Non-sales table keywords that must NEVER be parsed as sales/orders
        non_sales_keywords = [
            'quydoi', 'donviquydoi', 'don_vi_sp',
            'sanpham', 'san_pham', 'hanghoa', 'hang_hoa', 'danhmuc', 'danh_muc', 'danh_sach_hang_hoa',
            'thuoc',
            'tonkho', 'ton_kho', 'the_kho', 'lo_hang', 'donhang_lo', 'chi_tiet_don_lo',
            'banggia', 'bang_gia', 'lichsugia', 'lich_su_gia',
            'giaohang', 'giao_hang',
            'hanno', 'han_no', 'sono', 'so_no', 'sogop', 'so_gop', 'phieuthu', 'phieu_thu', 'thanhtoan', 'thanh_toan',
            'baocaoca', 'bao_cao_ca', 'chi_nhanh',
            'khachhang', 'khach_hang', 'nhanvien', 'nhan_vien', 'danhsachtho', 'nhacungcap', 'nha_cung_cap',
            'khuyenmai', 'khuyen_mai',
            'thumaycu', 'thu_may_cu', 'baohanh', 'bao_hanh', 'tra_hang', 'trahang',
            'ghi tắt', 'ghitat', 'ghi_tat'
        ]

        for fname, df in self.loader.data_files.items():
            fname_l = fname.lower()
            # Expenses
            if 'chiphi' in fname_l or 'chi_phi' in fname_l:
                exp_df = self.extract_expenses(df)
                if exp_df is not None:
                    expenses_dfs.append(exp_df)
                continue

            # Imports
            if any(k in fname_l for k in ['nhaphang', 'nhap_hang', 'phieu_nhap', 'chi_tiet_nhap', 'donnhaphang']):
                imp_df = self.extract_imports(fname_l, df)
                if imp_df is not None:
                    imports_dfs.append(imp_df)
                continue

            # Skip non-sales tables
            if any(k in fname_l for k in non_sales_keywords):
                continue

            # Check if this table has sales/order data
            order_data, item_data = self.extract_sales_table(df)
            if item_data is not None:
                items_dfs.append(item_data)
            if order_data is not None:
                orders_dfs.append(order_data)

        if orders_dfs:
            combined_ord = pd.concat(orders_dfs, ignore_index=True)
            if 'order_id' in combined_ord.columns:
                self.orders_df = combined_ord.drop_duplicates(subset=['order_id'], keep='first')
            else:
                self.orders_df = combined_ord.drop_duplicates()

        if items_dfs:
            combined = pd.concat(items_dfs, ignore_index=True)
            # Rulebook §145 & §146:
            # - Hai dòng của cùng một đơn giống nhau ở mọi ô là lỗi xuất file, tính một lần.
            # - Bảng không ghi theo đơn (has_explicit_order_id == False): hai dòng giống nhau trong cùng file vẫn tính cả hai!
            if '_has_order_id' in combined.columns and combined['_has_order_id'].all():
                dedup_cols = [c for c in ['order_id', 'item_code', 'item_name', 'unit', 'qty', 'unit_price', 'amount'] if c in combined.columns]
                if dedup_cols:
                    self.items_df = combined.drop_duplicates(subset=dedup_cols, keep='first').copy()
                else:
                    self.items_df = combined.copy()
            else:
                self.items_df = combined.copy()
        elif self.orders_df is not None and not self.orders_df.empty:
            # Shop only has order summary table without item details (e.g. dev_009 danh_sach_hoa_don.csv)
            self.is_order_summary_only = True
            synth = self.orders_df.copy()
            synth['item_name'] = 'Đơn hàng'
            synth['item_code'] = 'ORDER'
            synth['unit'] = 'đơn'
            synth['qty'] = 1.0
            synth['qty_base'] = 1.0
            synth['unit_price'] = synth['total_order']
            synth['amount'] = synth['total_order']
            synth['cost_price'] = 0.0
            self.items_df = synth

        if imports_dfs:
            self.imports_df = pd.concat(imports_dfs, ignore_index=True)
        if expenses_dfs:
            self.expenses_df = pd.concat(expenses_dfs, ignore_index=True)

        # Link orders status, date, product names, and unit conversions to items
        self.link_orders_and_items()

    def extract_abbreviations(self, df):
        cols = list(df.columns)
        c_short = self.find_col(cols, [r'ghi tắt', r'viết tắt', r'mã tắt', r'tên tắt', r'cách ghi'])
        c_full = self.find_col(cols, [r'tên đầy đủ', r'tên hàng', r'tên sản phẩm', r'đầy đủ'])
        if c_short and c_full:
            for _, row in df.iterrows():
                s = str(row[c_short]).strip()
                f = str(row[c_full]).strip()
                if s and f and s.lower() != 'nan' and f.lower() != 'nan':
                    self.abbrev_map[s.lower()] = f

    def extract_returned_orders(self, fname_l, df):
        cols = list(df.columns)
        c_ord = self.find_col(cols, [r'mã đơn hàng', r'mã đơn', r'madon', r'ma_don', r'số đơn', r'mã hđ'])
        if not c_ord:
            return
        if 'tra_hang' in fname_l or 'trahang' in fname_l:
            for val in df[c_ord].dropna().unique():
                self.returned_order_ids.add(str(val).strip())
        else:
            c_ret_qty = self.find_col(cols, [r'số lượng trả', r'sl trả', r'sl_tra', r'sltra'])
            if c_ret_qty:
                ret_rows = df[df[c_ret_qty].apply(clean_currency_str) > 0]
                for val in ret_rows[c_ord].dropna().unique():
                    self.returned_order_ids.add(str(val).strip())

    def extract_conversions(self, df):
        cols = list(df.columns)
        # Check inline "Quy đổi" column like "1 thùng = 4 bịch" in dev_013 QuanLy.xlsx:DanhMuc
        c_inline = self.find_col(cols, [r'^quy đổi$', r'^quy_doi$'])
        p_name = self.find_col(cols, [r'tên sản phẩm', r'tên hàng hóa', r'tên hàng', r'tên món', r'sản phẩm', r'^hàng$', r'mã sp', r'mã hàng', r'masp'])
        if c_inline and p_name:
            for _, row in df.iterrows():
                p = str(row[p_name]).strip()
                val = str(row[c_inline]).strip()
                m = re.search(r'1\s+([^\s=]+)\s*=\s*(\d+)\s+([^\s]+)', val)
                if m and p and p.lower() != 'nan':
                    u_l = m.group(1).strip().lower()
                    rate = float(m.group(2))
                    if p not in self.unit_conversions:
                        self.unit_conversions[p] = {}
                    self.unit_conversions[p][u_l] = rate

        u_conv = self.find_col(cols, [r'đơn vị quy đổi', r'đvt quy đổi', r'tên đvt', r'loại đv', r'đv nhập', r'ten_dvt', r'tendvt', r'đơn vị lớn', r'đơn vị'])
        u_rate = self.find_col(cols, [r'tỉ lệ quy đổi', r'tỷ lệ quy đổi', r'hệ số quy đổi', r'hệ số', r'giá trị quy đổi', r'he_so', r'heso', r'bằng'])
        if p_name and u_conv and u_rate:
            raw_entries = []
            for _, row in df.iterrows():
                p = str(row[p_name]).strip()
                unit = str(row[u_conv]).strip().lower()
                rate_raw = str(row[u_rate]).strip().lower()
                if not p or p.lower() == 'nan' or not unit or unit == 'nan':
                    continue
                if p not in self.unit_conversions:
                    self.unit_conversions[p] = {}
                try:
                    rate = float(rate_raw)
                    self.unit_conversions[p][unit] = rate
                except Exception:
                    # Handle relative unit like "10 vỉ" in dev_005
                    m_rel = re.match(r'^(\d+(?:[.,]\d+)?)\s+(.+)$', rate_raw)
                    if m_rel:
                        raw_entries.append((p, unit, float(m_rel.group(1).replace(',', '.')), m_rel.group(2).strip()))
            # Resolve relative units
            for p, unit, mult, sub_unit in raw_entries:
                sub_rate = self.unit_conversions.get(p, {}).get(sub_unit, 1.0)
                self.unit_conversions[p][unit] = mult * sub_rate

    def extract_products(self, df):
        cols = list(df.columns)
        c_code = self.find_col(cols, [r'mã sku', r'sku', r'mã hàng hóa', r'mã hàng', r'mã sản phẩm bán', r'mã sản phẩm', r'mã vạch', r'ma_sp', r'masp', r'id sp', r'ma_hang'])
        c_name = self.find_col(cols, [r'tên hàng hóa', r'tên sản phẩm', r'tên hàng', r'ten_hang', r'tensp', r'ten_sp', r'hàng bán', r'tên món', r'sản phẩm', r'món', r'^hàng$'])
        c_group = self.find_col(cols, [r'nhóm hàng hóa', r'nhóm hàng', r'nhóm', r'nhóm hàng\(3 cấp\)', r'nhom hang', r'nhom_hang', r'nhomsp', r'loại sản phẩm', r'loại hàng', r'phân loại'])
        c_unit = self.find_col(cols, [r'đơn vị tính', r'đơn vị cơ bản', r'đvt cơ bản', r'đơn vị gốc', r'đv gốc', r'dvt_co_ban', r'dvt', r'đvt', r'đơn vị'])
        c_price = self.find_col(cols, [r'giá bán', r'giá hiện tại', r'giá niêm yết', r'gia_ban', r'giá'])
        c_cost = self.find_col(cols, [r'giá vốn', r'giá vốn hiện tại', r'gia_von', r'giá nhập'])

        res = pd.DataFrame()
        if c_code:
            res['item_code'] = df[c_code].fillna('').astype(str).str.strip()
        elif c_name:
            res['item_code'] = df[c_name].fillna('').astype(str).str.strip()
        else:
            return

        res['item_name'] = df[c_name].fillna('').astype(str).str.strip() if c_name else res['item_code']
        res['item_group'] = df[c_group].fillna('').astype(str).str.strip() if c_group else ''
        res['base_unit'] = df[c_unit].fillna('').astype(str).str.strip().str.lower() if c_unit else ''
        res['price'] = df[c_price].apply(clean_currency_str) if c_price else 0.0
        res['cost_price'] = df[c_cost].apply(clean_currency_str) if c_cost else 0.0

        if self.products_df is None:
            self.products_df = res
        else:
            self.products_df = pd.concat([self.products_df, res], ignore_index=True).drop_duplicates(subset=['item_code'], keep='first')

    def extract_medicine_info(self, df):
        cols = list(df.columns)
        c_code = self.find_col(cols, [r'mã sku', r'sku', r'mã hàng', r'mã hh', r'mã sản phẩm', r'masp', r'ma_sp'])
        c_active = self.find_col(cols, [r'hoạt chất', r'hoạt chất chính', r'hoatchat', r'hoat_chat'])
        c_rx = self.find_col(cols, [r'thuốc kê đơn', r'kê đơn', r'bantheodon', r'ban_theo_don'])

        if not c_code:
            return

        med_df = pd.DataFrame()
        med_df['item_code'] = df[c_code].fillna('').astype(str).str.strip()
        med_df['active_ingredient'] = df[c_active].fillna('').astype(str).str.strip() if c_active else ''
        if c_rx:
            med_df['is_prescription'] = df[c_rx].apply(lambda x: str(x).strip().lower() in ['true', '1', '1.0', 'có', 'yes', 'kê đơn'])
        else:
            med_df['is_prescription'] = False

        if self.products_df is not None:
            for col in ['active_ingredient', 'is_prescription']:
                if col in self.products_df.columns:
                    self.products_df = self.products_df.drop(columns=[col])
            self.products_df = pd.merge(self.products_df, med_df, on='item_code', how='left')
        else:
            self.products_df = med_df

    def extract_sales_table(self, df):
        cols = list(df.columns)

        c_order_id = self.find_col(cols, [
            r'mã đơn hàng', r'mã hóa đơn', r'mã hoá đơn', r'mã đơn', r'order id',
            r'mã bill', r'^bill$', r'ma don', r'ma_don', r'madon', r'số hđ', r'mã chứng từ', r'số ct',
            r'mã giao dịch', r'mã hđ', r'so_hd', r'mahd', r'ma_hd', r'số đơn', r'invoice no',
            r'^hđ$', r'^hd$', r'^số_hđ$', r'^mã_hđ$', r'số phiếu', r'mã phiếu'
        ])
        # In phone shops (dev_011, dev_014), STT is the order number if there is no other order ID column and no "No." column
        if not c_order_id and not self.find_col(cols, [r'^no\.$', r'số dòng']):
            c_order_id = self.find_col(cols, [r'^stt$'])

        c_date = self.find_col(cols, [
            r'ngày đặt hàng', r'ngày bán', r'ngày giao dịch', r'ngày tháng', r'ngày xuất',
            r'thời gian tạo', r'thời gian', r'ngayban', r'ngay_ban', r'ngaydat', r'ngay_dat',
            r'ngày lập', r'ngày hđ', r'ngày chứng từ', r'ngày sửa', r'ngày', r'ngay', r'^date$'
        ])
        c_status = self.find_col(cols, [
            r'trạng thái đơn hàng', r'trạng thái đơn', r'trạng thái xử lý', r'trạng thái hđ',
            r'trạng thái', r'trang_thai', r'trangthai', r'kết quả', r'tiến độ', r'xử lý',
            r'status', r'tình trạng đơn', r'tình trạng', r'tt đơn', r'ghi chú tt', r'ghi chú', r'ghichu'
        ])
        c_item_name = self.find_col(cols, [
            r'tên hàng hóa', r'tên sản phẩm', r'tên hàng', r'tên mặt hàng', r'mặt hàng',
            r'hàng bán', r'chi tiết hàng', r'sản phẩm', r'món', r'nội dung', r'hàng hóa',
            r'^hàng$', r'ten_hang', r'ten_sp', r'tensp', r'làm gì', r'dịch vụ'
        ])
        c_item_code = self.find_col(cols, [
            r'mã hàng hóa', r'mã hàng', r'mã sản phẩm', r'mã h\.hóa', r'mã vạch',
            r'ma hang', r'ma_hang', r'mahang', r'ma_sp', r'masp', r'id sp', r'sku', r'mã sp', r'^mã$'
        ])
        c_qty = self.find_col(cols, [
            r'số lượng sản phẩm', r'số lượng bán', r'số lượng', r'sl bán', r'sluong',
            r'soluong', r'so_luong', r'slg', r'^sl$'
        ])
        c_qty_base = self.find_col(cols, [
            r'số lượng quy đổi', r'quy ra đv lẻ', r'sl quy đổi', r'sl_quy_doi', r'slquydoi',
            r'sl cơ bản', r'số lượng lẻ', r'sl lẻ', r'sl quy ra lẻ'
        ])
        c_unit = self.find_col(cols, [
            r'đơn vị tính', r'đơn vị \(tính\)', r'đvt', r'dvt', r'đơn vị', r'đ\.vị', r'unit', r'^đvt$', r'^dvt$'
        ])
        c_price = self.find_col(cols, [
            r'giá sản phẩm', r'đơn giá \(đ\)', r'đơn giá', r'giá 1 đv', r'giá bán',
            r'giá đơn vị', r'giá niêm yết', r'don_gia', r'dongia', r'giá lẻ', r'giá', r'^đg$', r'^dg$', r'tiền công'
        ])
        c_amount = self.find_col(cols, [
            r'thành tiền sau giảm', r'thành tiền vnd', r'thành tiền', r'thanh tien', r'thanh_tien', r'thanhtien',
            r't\.tiền', r'trị giá', r'trigia', r'tri_gia', r'^amount$', r'^cộng$', r'^tt$', r'^tiền$',
            r'giá trị hàng', r'tổng tiền'
        ])
        c_total_order = self.find_col(cols, [
            r'giá trị hàng', r'tổng tiền hàng', r'tổng phiếu', r'tổng bill', r'tong tien', r'tongtien',
            r'tổng tiền', r'tổng cộng', r'tổng thanh toán', r'tongthanhtoan', r'tổng đơn', r'thành tiền', r'^tiền$'
        ])
        c_cust_id = self.find_col(cols, [
            r'mã khách hàng', r'mã khách mua', r'mã người mua', r'mã kh mua', r'mã kh', r'mã thẻ',
            r'customer id', r'ma_kh', r'makh', r'số kh'
        ])
        c_cust_name = self.find_col(cols, [
            r'tên khách hàng', r'khách hàng \(tên\)', r'họ tên khách', r'khách hàng',
            r'tên kh', r'đối tác', r'ten_kh', r'tenkh', r'khách', r'^kh$', r'xe', r'biển số'
        ])
        c_payment = self.find_col(cols, [
            r'phương thức tt', r'thanh toán bằng', r'phương thức', r'hình thức thanh toán', r'hinh_thuc_tt', r'pttt'
        ])
        c_debt = self.find_col(cols, [r'còn nợ', r'con no', r'con_no', r'^nợ$', r'còn lại'])
        c_paid = self.find_col(cols, [r'khách đã trả', r'khách trả', r'đã thu', r'đã thanh toán'])
        c_cost = self.find_col(cols, [r'giá vốn lúc bán', r'giavonlucban', r'giá vốn'])
        c_note = self.find_col(cols, [r'ghi chú', r'ghichu', r'note'])
        c_time = self.find_col(cols, [r'^giờ$', r'giờ bán', r'giờ đặt', r'^gio$'])

        if not c_order_id and not c_date:
            return None, None

        # Build order dataframe
        order_df = pd.DataFrame()
        has_explicit_order_id = c_order_id is not None
        if c_order_id:
            if c_order_id.strip().lower() == 'stt' and '_sheet_name' in df.columns:
                order_df['order_id'] = df['_sheet_name'].fillna('').astype(str).str.strip() + '_' + df[c_order_id].fillna('').astype(str).str.strip()
            else:
                order_df['order_id'] = df[c_order_id].fillna('').astype(str).str.strip()
        else:
            order_df['order_id'] = [f"auto_{i}" for i in range(len(df))]

        fallback_yr = None
        if '_sheet_year' in df.columns:
            valid_yrs = df['_sheet_year'].dropna()
            if not valid_yrs.empty:
                fallback_yr = int(valid_yrs.iloc[0])
        if not fallback_yr and self.loader.max_data_date:
            fallback_yr = int(self.loader.max_data_date[:4])

        if c_date:
            if '_sheet_year' in df.columns:
                order_df['date'] = [
                    parse_date_str(v, default_year=(int(sy) if pd.notna(sy) else fallback_yr))
                    for v, sy in zip(df[c_date], df['_sheet_year'])
                ]
            else:
                order_df['date'] = df[c_date].apply(lambda x: parse_date_str(x, default_year=fallback_yr))
            if c_time and c_time != c_date:
                order_df['raw_date'] = df[c_date].fillna('').astype(str) + ' ' + df[c_time].fillna('').astype(str)
            else:
                order_df['raw_date'] = df[c_date].fillna('').astype(str)
        else:
            order_df['date'] = None
            order_df['raw_date'] = df[c_time].fillna('').astype(str) if c_time else ''

        # Combine status and note if needed (Rulebook §143: Sổ không có cột trạng thái -> trạng thái ghi ở cột Ghi chú)
        if c_status and c_note and c_status != c_note:
            s_col = df[c_status].fillna('').astype(str).str.strip()
            n_col = df[c_note].fillna('').astype(str).str.strip()
            order_df['status'] = [f"{s} | {n}" if n else s for s, n in zip(s_col, n_col)]
        elif c_status:
            order_df['status'] = df[c_status].fillna('').astype(str).str.strip()
        else:
            order_df['status'] = None

        order_df['cust_id'] = df[c_cust_id].fillna('').astype(str).str.strip() if c_cust_id else ''
        order_df['cust_name'] = df[c_cust_name].fillna('').astype(str).str.strip() if c_cust_name else ''
        order_df['payment_method'] = df[c_payment].fillna('').astype(str).str.strip() if c_payment else ''
        order_df['total_order'] = df[c_total_order].apply(clean_currency_str) if c_total_order else 0.0
        order_df['debt'] = df[c_debt].apply(clean_currency_str) if c_debt else 0.0
        order_df['paid'] = df[c_paid].apply(clean_currency_str) if c_paid else 0.0

        # Build items dataframe
        item_df = pd.DataFrame()
        item_df['order_id'] = order_df['order_id']
        item_df['_has_order_id'] = has_explicit_order_id
        item_df['date'] = order_df['date']
        item_df['raw_date'] = order_df['raw_date']
        item_df['status'] = order_df['status']
        item_df['cust_id'] = order_df['cust_id']
        item_df['cust_name'] = order_df['cust_name']

        if c_item_name:
            item_df['item_name'] = df[c_item_name].fillna('').astype(str).apply(lambda x: re.sub(r'\s+', ' ', '' if pd.isna(x) else str(x)).strip())
        elif c_item_code:
            item_df['item_name'] = df[c_item_code].fillna('').astype(str).apply(lambda x: re.sub(r'\s+', ' ', '' if pd.isna(x) else str(x)).strip())
        else:
            # Table is orders summary only (e.g. don_hang.csv or danh_sach_hoa_don.csv)
            return order_df, None

        # Expand abbreviations if present (dev_011, dev_014 "Ghi tắt")
        if self.abbrev_map:
            item_df['item_name'] = item_df['item_name'].apply(lambda x: self.abbrev_map.get(str(x).lower().strip(), x))

        item_df['item_code'] = df[c_item_code].fillna('').astype(str).apply(lambda x: re.sub(r'\s+', ' ', '' if pd.isna(x) else str(x)).strip()) if c_item_code else item_df['item_name']
        item_df['unit'] = df[c_unit].fillna('').astype(str).str.strip().str.lower() if c_unit else ''
        item_df['qty'] = df[c_qty].apply(clean_currency_str) if c_qty else 1.0

        # Detect negative quantity return rows (Rulebook dev_004 & biz_105: "đơn có dòng số lượng âm là đơn có trả hàng")
        neg_qty_mask = item_df['qty'] < 0
        if neg_qty_mask.any():
            for oid in item_df.loc[neg_qty_mask, 'order_id'].dropna().unique():
                self.returned_order_ids.add(str(oid).strip())
            # Remove negative return rows from items_df (and the entire order will be marked 'trả hàng')
            item_df = item_df[~neg_qty_mask].copy()

        # Filter out non-product adjustment rows in item_df:
        # e.g. dev_013 "Voucher", "Dùng <n> điểm", "Phí ship", and empty/nan item names
        non_item_pat = re.compile(
            r'^(nan|none|null|voucher|dùng\s+\d+\s*điểm|phí\s*ship|phí\s*giao\s*hàng|phí\s*vận\s*chuyển|giảm\s*giá\s*hđ|chiết\s*khấu\s*hđ)$',
            re.IGNORECASE
        )
        valid_item_mask = item_df['item_name'].apply(lambda x: bool(str(x).strip()) and not bool(non_item_pat.match(str(x).strip())))
        item_df = item_df[valid_item_mask].copy()

        if c_qty_base:
            item_df['qty_base'] = df.loc[item_df.index, c_qty_base].apply(clean_currency_str)
        else:
            item_df['qty_base'] = 0.0

        item_df['unit_price'] = df.loc[item_df.index, c_price].apply(clean_currency_str) if c_price else 0.0
        if c_amount:
            item_df['amount'] = df.loc[item_df.index, c_amount].apply(clean_currency_str)
        else:
            item_df['amount'] = item_df['qty'] * item_df['unit_price']

        item_df['cost_price'] = df.loc[item_df.index, c_cost].apply(clean_currency_str) if c_cost else 0.0

        # Auto-detect if amounts/prices are in thousands (e.g. 29.0 instead of 29000)
        if not item_df.empty:
            med_price = item_df[item_df['unit_price'] > 0]['unit_price'].median()
            if 0 < med_price < 500:
                item_df['unit_price'] *= 1000.0
                item_df['amount'] *= 1000.0
                item_df['cost_price'] *= 1000.0

        if not order_df.empty:
            med_tot = order_df[order_df['total_order'] > 0]['total_order'].median()
            if 0 < med_tot < 500:
                order_df['total_order'] *= 1000.0
                order_df['debt'] *= 1000.0
                order_df['paid'] *= 1000.0

        return order_df, item_df

    def extract_imports(self, fname_l, df):
        cols = list(df.columns)
        c_code = self.find_col(cols, [r'mã nhập hàng', r'mã đơn nhập', r'mã phiếu nhập', r'số phiếu', r'mã nhập', r'maphieunhap', r'ma_phieu_nhap', r'so_phieu'])
        c_date = self.find_col(cols, [r'ngày nhập', r'ngaynhap', r'ngay_nhap', r'thời gian', r'ngày', r'ngày chi'])
        c_line_amt = self.find_col(cols, [r'thành tiền', r'thanh tien', r'thanh_tien', r'thanhtien'])
        c_receipt_tot = self.find_col(cols, [r'tổng giá trị nhập', r'tonggiatrinhap', r'tổng tiền phiếu', r'cần trả ncc', r'tổng tiền', r'so tien', r'giá trị'])
        c_status = self.find_col(cols, [r'trạng thái phiếu', r'trạng thái', r'trangthai', r'trang_thai'])
        c_supplier = self.find_col(cols, [r'mã nhà cung cấp', r'mã ncc', r'mancc', r'ma_ncc', r'nhà cung cấp', r'ncc'])
        c_item_name = self.find_col(cols, [r'tên mặt hàng', r'tên hàng', r'tên sản phẩm', r'hàng hóa', r'^hàng$', r'product', r'mô tả', r'sản phẩm', r'ten_hang', r'ten_sp'])
        c_item_code = self.find_col(cols, [r'mã sku', r'mã hàng bán', r'mã vạch', r'mã hàng', r'mã sp', r'masp', r'ma_sp'])
        c_qty = self.find_col(cols, [r'số lượng nhập', r'sl nhập', r'sl_nhap', r'slnhap', r'số lượng', r'^sl$'])
        c_price = self.find_col(cols, [r'đơn giá nhập', r'dongianhap', r'giá nhập', r'gianhap', r'đơn giá', r'don_gia'])
        c_unit = self.find_col(cols, [r'đvt nhập', r'dvt_nhap', r'đơn vị nhập', r'đvt', r'dvt', r'đơn vị'])

        # Skip header-only DonNhapHang.xlsx if DonNhapHang_ChiTiet or phieu_nhap has no amounts and no prices
        if not c_line_amt and not c_receipt_tot and not c_price:
            return None

        imp_df = pd.DataFrame()
        if c_code:
            imp_df['import_id'] = df[c_code].fillna('').astype(str).str.strip()
        else:
            imp_df['import_id'] = [f"imp_{i}" for i in range(len(df))]

        fallback_yr = None
        if '_sheet_year' in df.columns:
            valid_yrs = df['_sheet_year'].dropna()
            if not valid_yrs.empty:
                fallback_yr = int(valid_yrs.iloc[0])
        if not fallback_yr and self.loader.max_data_date:
            fallback_yr = int(self.loader.max_data_date[:4])

        imp_df['date'] = df[c_date].apply(lambda x: parse_date_str(x, default_year=fallback_yr)) if c_date else None
        imp_df['status'] = df[c_status].fillna('').astype(str).str.strip().str.lower() if c_status else 'hoàn thành'
        imp_df['supplier'] = df[c_supplier].fillna('').astype(str).str.strip() if c_supplier else ''
        imp_df['item_name'] = df[c_item_name].fillna('').astype(str).str.strip() if c_item_name else ''
        imp_df['item_code'] = df[c_item_code].fillna('').astype(str).str.strip() if c_item_code else imp_df['item_name']
        imp_df['unit'] = df[c_unit].fillna('').astype(str).str.strip().str.lower() if c_unit else ''
        imp_df['qty'] = df[c_qty].apply(clean_currency_str) if c_qty else 1.0
        imp_df['unit_price'] = df[c_price].apply(clean_currency_str) if c_price else 0.0

        if c_line_amt:
            imp_df['total_amount'] = df[c_line_amt].apply(clean_currency_str)
            imp_df['_is_receipt_total'] = False
        elif c_price and c_qty:
            imp_df['total_amount'] = imp_df['qty'] * imp_df['unit_price']
            imp_df['_is_receipt_total'] = False
        elif c_receipt_tot:
            imp_df['total_amount'] = df[c_receipt_tot].apply(clean_currency_str)
            imp_df['_is_receipt_total'] = True
        else:
            imp_df['total_amount'] = 0.0
            imp_df['_is_receipt_total'] = False

        return imp_df

    def extract_expenses(self, df):
        cols = list(df.columns)
        c_date = self.find_col(cols, [r'ngày chi', r'ngày', r'thời gian', r'tháng'])
        c_cat = self.find_col(cols, [r'mục', r'khoản chi', r'loại chi phí', r'nội dung'])
        c_amt = self.find_col(cols, [r'tiền chi', r'so tien', r'số tiền', r'tiền'])

        if not c_amt:
            return None

        fallback_yr = None
        if '_sheet_year' in df.columns:
            valid_yrs = df['_sheet_year'].dropna()
            if not valid_yrs.empty:
                fallback_yr = int(valid_yrs.iloc[0])
        if not fallback_yr and self.loader.max_data_date:
            fallback_yr = int(self.loader.max_data_date[:4])

        exp_df = pd.DataFrame()
        exp_df['date'] = df[c_date].apply(lambda x: parse_date_str(x, default_year=fallback_yr)) if c_date else None
        exp_df['category'] = df[c_cat].fillna('').astype(str).str.strip() if c_cat else ''
        exp_df['amount'] = df[c_amt].apply(clean_currency_str)
        return exp_df

    def link_orders_and_items(self):
        if self.items_df is not None and self.orders_df is not None:
            ords = self.orders_df.dropna(subset=['order_id']).copy()
            def status_priority(s):
                if not s or pd.isna(s):
                    return 0
                s_l = str(s).lower().strip()
                if s_l.isdigit() or s_l == 'none' or s_l == 'nan':
                    return 0
                if any(k in s_l for k in ['huỷ', 'hủy', 'trả', 'tra hang', 'hoàn hàng', 'hoàn trả']) or re.search(r'\bhuy\b', s_l):
                    return 3
                if any(k in s_l for k in ['hoàn thành', 'đã giao', 'thành công', 'đã lấy', 'vận chuyển', 'đang giao']):
                    return 2
                return 1

            ords['has_date'] = ords['date'].notna() & (ords['date'] != '')
            ords['priority'] = ords['status'].apply(status_priority)
            ords = ords.sort_values(by=['has_date', 'priority'], ascending=[False, False]).drop_duplicates('order_id')

            date_map = dict(zip(ords['order_id'], ords['date']))
            raw_date_map = dict(zip(ords['order_id'], ords['raw_date']))
            status_map = dict(zip(ords['order_id'], ords['status']))
            cust_id_map = dict(zip(ords['order_id'], ords['cust_id']))
            cust_name_map = dict(zip(ords['order_id'], ords['cust_name']))

            self.items_df = self.items_df.copy()
            mapped_date = self.items_df['order_id'].map(date_map)
            if 'date' in self.items_df:
                self.items_df['date'] = self.items_df['date'].replace('', pd.NA).fillna(mapped_date)
            else:
                self.items_df['date'] = mapped_date
            self.items_df['date'] = self.items_df['date'].fillna('').astype(str)

            mapped_raw = self.items_df['order_id'].map(raw_date_map)
            if 'raw_date' in self.items_df:
                self.items_df['raw_date'] = self.items_df['raw_date'].replace('', pd.NA).fillna(mapped_raw)
            else:
                self.items_df['raw_date'] = mapped_raw

            mapped_status = self.items_df['order_id'].map(status_map)
            if 'status' in self.items_df:
                self.items_df['status'] = self.items_df['status'].replace({'None': pd.NA, 'nan': pd.NA, '': pd.NA}).fillna(mapped_status)
            else:
                self.items_df['status'] = mapped_status

            self.items_df['status'] = self.items_df['status'].fillna('hoàn thành')

            # Mark orders in self.returned_order_ids as 'trả hàng' in items_df so they are excluded from revenue/AOV/order_count
            if self.returned_order_ids:
                ret_mask = self.items_df['order_id'].isin(self.returned_order_ids)
                self.items_df.loc[ret_mask, 'status'] = 'trả hàng'

            if 'cust_id' not in self.items_df or (self.items_df['cust_id'] == '').all():
                self.items_df['cust_id'] = self.items_df['order_id'].map(cust_id_map).fillna('')
            if 'cust_name' not in self.items_df or (self.items_df['cust_name'] == '').all():
                self.items_df['cust_name'] = self.items_df['order_id'].map(cust_name_map).fillna('')

        # Map item_code -> item_name and item_group from self.products_df if available (e.g. dev_006, dev_010)
        if self.items_df is not None and not self.items_df.empty and self.products_df is not None and not self.products_df.empty:
            code_to_name = dict(zip(self.products_df['item_code'], self.products_df['item_name']))
            code_to_group = dict(zip(self.products_df['item_code'], self.products_df['item_group']))
            name_to_code = dict(zip(self.products_df['item_name'].str.lower(), self.products_df['item_code']))
            name_to_group = dict(zip(self.products_df['item_name'].str.lower(), self.products_df['item_group']))

            # If item_name is same as item_code and item_code is in code_to_name, replace with real item_name
            self.items_df['item_name'] = [
                code_to_name.get(c, n) if (n == c or not n or n == 'nan') else n
                for c, n in zip(self.items_df['item_code'], self.items_df['item_name'])
            ]
            # If item_code is same as item_name and item_name is in name_to_code, populate real SKU item_code
            self.items_df['item_code'] = [
                name_to_code.get(str(n).lower().strip(), c) if c == n else c
                for c, n in zip(self.items_df['item_code'], self.items_df['item_name'])
            ]
            self.items_df['item_group'] = [
                code_to_group.get(c) or name_to_group.get(str(n).lower().strip(), '')
                for c, n in zip(self.items_df['item_code'], self.items_df['item_name'])
            ]

        # Also map item_code -> item_name in imports_df if imports only recorded SKU (e.g. dev_005, dev_006)
        if self.imports_df is not None and not self.imports_df.empty and self.products_df is not None and not self.products_df.empty:
            code_to_name = dict(zip(self.products_df['item_code'], self.products_df['item_name']))
            self.imports_df['item_name'] = [
                code_to_name.get(c, n) if (not n or n == c or n == 'nan') else n
                for c, n in zip(self.imports_df['item_code'], self.imports_df['item_name'])
            ]

        # Compute qty_base using self.unit_conversions whenever qty_base is not already converted!
        if self.items_df is not None and not self.items_df.empty:
            conv_lower = {k.lower().strip(): v for k, v in self.unit_conversions.items()}
            def calc_base_qty(row):
                qb = row.get('qty_base', 0.0)
                q = row.get('qty', 0.0)
                if qb and qb > 0 and qb != q:
                    return qb
                unit = str(row.get('unit', '')).lower().strip()
                if not unit:
                    return q if q > 0 else qb
                iname = str(row.get('item_name', '')).lower().strip()
                icode = str(row.get('item_code', '')).lower().strip()
                u_map = conv_lower.get(iname) or conv_lower.get(icode)
                if u_map:
                    if unit in u_map:
                        return q * u_map[unit]
                    for uk, urate in u_map.items():
                        if uk.startswith(unit + ' ') or unit.startswith(uk + ' '):
                            return q * urate
                return qb if qb > 0 else q
            self.items_df['qty_base'] = self.items_df.apply(calc_base_qty, axis=1)

import re
from datamate.time_parser import TimeParser

class QuestionParser:
    def __init__(self, calendar_resolver=None, slm_parser=None):
        self.time_parser = TimeParser(calendar_resolver)
        self.slm_parser = slm_parser

    def parse(self, q_text, as_of_date):
        t = q_text.lower().strip()
        t = re.sub(r'\b(mik|mk)\b', 'mình', t)
        t = re.sub(r'\b(z|v|zậy|zay|vaayj)\b', 'vậy', t)
        t = re.sub(r'\b(bnhiu|bnhieu|bnh|bn|bao nhiu)\b', 'bao nhiêu', t)
        t = re.sub(r'\b(nhìu|nhiu)\b', 'nhiều', t)
        t = re.sub(r'\b(jum|giup|gium)\b', 'giúp', t)
        t = re.sub(r'\b(dthu|d thu|revenue)\b', 'doanh thu', t)
        t = re.sub(r'\b(hom ni|hôm ni|hnay|bữa nay|bua nay)\b', 'hôm nay', t)
        t = re.sub(r'\b(hqua|bữa qua|bua qua)\b', 'hôm qua', t)
        t = re.sub(r'\b(thangs|thang)\b', 'tháng', t)
        t = re.sub(r'\b(heets|het)\b', 'hết', t)
        t = re.sub(r'\bees\b', 'ế', t)
        t = re.sub(r'\btieenf\b', 'tiền', t)
        t = re.sub(r'\bddieenj\b', 'điện', t)
        t = re.sub(r'\b(laf|laff)\b', 'là', t)
        t = re.sub(r'\bngayf\b', 'ngày', t)
        t = re.sub(r'\bnhaats\b', 'nhất', t)
        t = re.sub(r'\bcuwar\b', 'cửa', t)
        t = re.sub(r'\bcuar\b', 'của', t)
        t = re.sub(r'\bbans\b', 'bán', t)
        t = re.sub(r'\bddown\b', 'đơn', t)
        t = re.sub(r'\bnhaf\b', 'nhà', t)
        t = re.sub(r'\btotal\b', 'tổng cộng', t)
        t = re.sub(r'\bmat hang\b', 'mặt hàng', t)
        t = re.sub(r'\bban chay\b', 'bán chạy', t)
        t = re.sub(r'\bchay nhat\b', 'chạy nhất', t)
        t = re.sub(r'\b(\d+)\s+mon\b', r'\1 món', t)
        t = re.sub(r'\bth(\d{1,2})[/-](\d{2,4})\b', r'tháng \1/\2', t)
        t = re.sub(r'\bt(\d{1,2})[/-](\d{2,4})\b', r'tháng \1/\2', t)
        text_lower = t

        time_info = self.time_parser.parse_time_period(text_lower, as_of_date)

        # 0. Continuous revenue increase (boolean) — check BEFORE unanswerable so "tháng sau luôn cao hơn tháng trước" is not mistaken for future prediction
        if any(k in text_lower for k in ['tăng liên tục', 'tang lien tuc', 'luôn cao hơn', 'luon cao hon', 'tháng sau cao hơn tháng trước']):
            periods = []
            # Check range "từ tháng M1/Y1 đến tháng M2/Y2"
            m_rng = re.search(r'từ\s*tháng\s*(\d{1,2})[/-](\d{4})\s*(?:đến|tới)\s*tháng\s*(\d{1,2})[/-](\d{4})', text_lower)
            if m_rng:
                m1, y1, m2, y2 = int(m_rng.group(1)), int(m_rng.group(2)), int(m_rng.group(3)), int(m_rng.group(4))
                cy, cm = y1, m1
                while (cy, cm) <= (y2, m2) and len(periods) < 24:
                    periods.append(self.time_parser.parse_time_period(f"tháng {cm}/{cy}", as_of_date))
                    cm += 1
                    if cm > 12:
                        cm = 1
                        cy += 1
            else:
                months = re.findall(r'(\d{1,2}[/-]\d{4})', text_lower)
                if len(months) < 2:
                    months = re.findall(r'tháng\s*(\d{1,2}(?:[\s/-]+\d{4})?)', text_lower)
                for m in months:
                    p = self.time_parser.parse_time_period(f"tháng {m}", as_of_date)
                    periods.append(p)
            return {
                'intent': 'continuous_increase',
                'ans_type': 'boolean',
                'periods': periods,
                'time_info': time_info
            }

        # 0b. Multi-month revenue list ("doanh thu N tháng liền nhau kết thúc ở tháng M/Y là bao nhiêu mỗi tháng")
        m_mlist = re.search(r'(\d+)\s*tháng\s*liền\s*nhau\s*kết\s*thúc\s*ở\s*tháng\s*(\d{1,2})[/-](\d{4})', text_lower)
        if m_mlist and any(k in text_lower for k in ['mỗi tháng', 'từng tháng']):
            n_m = int(m_mlist.group(1))
            end_m = int(m_mlist.group(2))
            end_y = int(m_mlist.group(3))
            m_periods = []
            cy, cm = end_y, end_m
            for _ in range(n_m):
                m_periods.append(self.time_parser.parse_time_period(f"tháng {cm}/{cy}", as_of_date))
                cm -= 1
                if cm < 1:
                    cm = 12
                    cy -= 1
            if 'từ tháng mới' not in text_lower:
                m_periods.reverse()
            return {
                'intent': 'monthly_revenue_list',
                'ans_type': 'list',
                'periods': m_periods,
                'time_info': time_info
            }

        # 0c. Highest revenue month ("từ tháng M1/Y1 tới tháng M2/Y2, tháng nào cửa hàng bán được nhiều tiền nhất")
        m_hmonth = re.search(r'từ\s*tháng\s*(\d{1,2})[/-](\d{4})\s*(?:đến|tới)\s*tháng\s*(\d{1,2})[/-](\d{4})', text_lower)
        if m_hmonth and 'tháng nào' in text_lower and any(k in text_lower for k in ['nhiều tiền nhất', 'doanh thu cao nhất', 'doanh thu nhiều nhất', 'cao nhất']):
            m1, y1, m2, y2 = int(m_hmonth.group(1)), int(m_hmonth.group(2)), int(m_hmonth.group(3)), int(m_hmonth.group(4))
            m_periods = []
            cy, cm = y1, m1
            while (cy, cm) <= (y2, m2) and len(m_periods) < 24:
                p = self.time_parser.parse_time_period(f"tháng {cm}/{cy}", as_of_date)
                p['month_label'] = f"{cy:04d}-{cm:02d}"
                m_periods.append(p)
                cm += 1
                if cm > 12:
                    cm = 1
                    cy += 1
            return {
                'intent': 'highest_revenue_month',
                'ans_type': 'date',
                'periods': m_periods,
                'time_info': time_info
            }

        # 0d. Number of days reaching revenue threshold ("có bao nhiêu ngày đạt doanh thu từ X đồng trở lên")
        m_days_rev = re.search(r'(?:bao nhiêu|mấy|đếm\s*số)\s*ngày\s*(?:đạt\s*|có\s*)?doanh\s*thu\s*(?:từ|trên)\s*([\d\.,]+)\s*(?:đồng|đ|k|triệu|tr)?\s*(?:trở\s*lên)?', text_lower)
        if m_days_rev:
            from datamate.data_loader import clean_currency_str
            thresh = clean_currency_str(m_days_rev.group(0))
            return {
                'intent': 'days_above_revenue',
                'ans_type': 'number',
                'min_revenue': thresh,
                'time_info': time_info
            }

        # 0e. Unit price of an item on a specific date ("Ngày DD/MM/YYYY cửa hàng bán X (với đơn) giá bao nhiêu một ...")
        m_uprice = re.search(r'bán\s+(.+?)\s+(?:với\s+)?(?:đơn\s+giá|giá\s+bán|giá)\s+bao nhiêu\s*(?:tiền\s*)?(?:một|1)\s+[^\s\?\.]+', text_lower)
        if m_uprice and not any(k in text_lower for k in ['trung bình', 'bình quân']):
            cand_item = m_uprice.group(1).strip()
            cand_item = re.sub(r'^(?:mặt hàng|sản phẩm|món)\s+', '', cand_item).strip()
            return {
                'intent': 'item_unit_price',
                'ans_type': 'number',
                'item_query': cand_item,
                'time_info': time_info
            }

        # 0f. Top K customers by spending ("Liệt kê 3 khách chi tiêu nhiều nhất", "Top 3 khách hàng mua nhiều tiền nhất", "ai mua hàng nhiều tiền nhất")
        if any(k in text_lower for k in [
            'khách chi tiêu nhiều nhất', 'khách hàng chi tiêu nhiều nhất',
            'khách mua nhiều tiền nhất', 'khách hàng mua nhiều tiền nhất',
            'ai mua hàng nhiều tiền nhất', 'ai chi tiêu nhiều nhất', 'khách nào mua nhiều tiền nhất',
            'khach chi tieu nhieu nhat', 'khach mua nhieu tien nhat'
        ]) and not any(k in text_lower for k in ['bao nhiêu tiền', 'mỗi người', 'từng người']):
            m_top_c = re.search(r'(\d+)\s*(?:khách|người)', text_lower)
            k = int(m_top_c.group(1)) if m_top_c and int(m_top_c.group(1)) > 1 else 1
            return {
                'intent': 'top_customers',
                'ans_type': 'list' if k > 1 else 'string',
                'k': k,
                'time_info': time_info
            }

        # 1. Check for unanswerable questions (Rulebook §7 & §98)
        if any(k in text_lower for k in [
            'nhà ở đường', 'địa chỉ nhà', 'quận mấy', 'phường nào', 'cách bao xa', 'số điện thoại', 'sdt',
            'dự đoán', 'dự báo', 'ngày mai', 'tuần sau', 'năm sau', 'tương lai', 'thời tiết'
        ]) or ('tháng sau' in text_lower and 'tháng trước' not in text_lower):
            return {
                'intent': 'unanswerable',
                'ans_type': 'unanswerable',
                'reason': 'Dữ liệu không lưu thông tin này hoặc yêu cầu dự đoán tương lai',
                'time_info': time_info
            }

        # 1b. Check AOV (Rulebook §50: Giá trị trung bình một đơn)
        if any(k in text_lower for k in [
            'trung bình mỗi đơn', 'trung bình mổi đơn', 'trung bình 1 đơn', 'trung bình một đơn',
            'trung bình trên một đơn', 'trung bình trên 1 đơn',
            'trung binh moi don', 'trung binh 1 don', 'trung binh mot don', 'trung binh tren mot don',
            'bình quân 1 đơn', 'bình quân một đơn', 'bình quân mỗi đơn', 'bình quân 1 phiếu', 'binh quan 1 don',
            'trung bình 1 hoá đơn', 'trung bình 1 hoa don', 'trung bình 1 hóa đơn', 'trung bình một phiếu'
        ]) and not any(k in text_lower for k in ['khách nào', 'nhân viên nào', 'thợ nào']):
            return {
                'intent': 'aov',
                'ans_type': 'number',
                'time_info': time_info
            }

        # 2. Prescription questions (Rulebook §95)
        if any(k in text_lower for k in ['kê đơn', 'ke don', 'thuốc kê']):
            if any(k in text_lower for k in ['chiếm', 'chiem', 'tỉ lệ', 'tỷ lệ', 'ti le', 'ty le', '%', 'phần trăm', 'phan tram']):
                return {
                    'intent': 'prescription_rate',
                    'ans_type': 'percent',
                    'time_info': time_info
                }
            elif any(k in text_lower for k in ['tiền', 'doanh thu', 'thu về', 'bán được']):
                return {
                    'intent': 'prescription_rev',
                    'ans_type': 'number',
                    'time_info': time_info
                }

        # 3. Cancellation questions (Rulebook §19)
        if (any(k in text_lower for k in ['huỷ', 'hủy']) or re.search(r'\b(huy don|don huy|bi huy|da huy|trang thai huy|huy)\b', text_lower)) and not any(k in text_lower for k in ['thợ', 'tho', 'khách', 'khach', 'nhân viên', 'nhan vien', 'phiếu nhập']):
            if any(k in text_lower for k in ['chiếm', 'chiem', 'tỉ lệ', 'tỷ lệ', 'ti le', 'ty le', '%', 'phần trăm', 'phan tram']):
                return {
                    'intent': 'cancellation_rate',
                    'ans_type': 'percent',
                    'time_info': time_info
                }
            elif re.search(r'(?:huỷ|hủy|huy).*(?:mấy|may|bao nhiêu)\s*(?:đơn|don)|(?:mấy|may|bao nhiêu|đếm|dem|số|so)\s*(?:đơn|don|đơn hàng).*(?:huỷ|hủy|huy)', text_lower):
                return {
                    'intent': 'cancelled_order_count',
                    'ans_type': 'number',
                    'time_info': time_info
                }

        # 4. Unsold items (Hàng ế) (Rulebook §77) — check BEFORE best_sellers so "bán ko đc món nào" doesn't trigger best_sellers!
        if any(k in text_lower for k in ['không bán được', 'khong ban duoc', 'bán chậm nhất', 'bán ko dc', 'bán ko được', 'không ai mua', 'khong ai mua']) or re.search(r'\b(hàng ế|hang e|bị ế|bi e|mặt hàng ế|mat hang e|ế ẩm|e am)\b', text_lower):
            if any(k in text_lower for k in ['bao nhiêu mặt hàng', 'mấy mặt hàng', 'mấy sp', 'bao nhiêu sp', 'mấy món', 'bao nhiêu món', 'có bao nhiêu', 'tổng cộng mấy']):
                return {
                    'intent': 'unsold_items_count',
                    'ans_type': 'number',
                    'time_info': time_info
                }
            return {
                'intent': 'unsold_items',
                'ans_type': 'list',
                'time_info': time_info
            }

        # 5. Best sellers / mặt hàng bán chạy nhất (Rulebook §45)
        if any(k in text_lower for k in [
            'bán chạy nhất', 'bán nhiều nhất', 'bán được nhiều nhất', 'chạy nhất', 'hot nhất',
            'nhiều ly nhất', 'nhiều chai nhất', 'nhiều gói nhất', 'nhiều cái nhất', 'bán chạy',
            'ban chay nhat', 'chay nhat', 'ban nhieu nhat'
        ]) and not any(k in text_lower for k in ['ưa chuộng', 'ua chuong', 'lời nhất', 'loi nhat', 'doanh thu cao nhất', 'nhiều tiền nhất']):
            m_top = re.search(r'\btop\s*(\d+)', text_lower)
            if not m_top:
                m_top = re.search(r'(\d+)\s*(?:món|mặt hàng|sản phẩm|sp|hàng|mon)\s*(?:bán chạy|bán nhiều|hot|chạy nhất)', text_lower)
            k = int(m_top.group(1)) if m_top and int(m_top.group(1)) > 1 else 1
            m_grp = re.search(r'nhóm\s+([^\s,\?]+)', text_lower)
            item_group = m_grp.group(1).strip() if m_grp else None
            return {
                'intent': 'best_sellers',
                'ans_type': 'list' if k > 1 else 'string',
                'k': k,
                'item_group': item_group,
                'time_info': time_info
            }

        # 6. Highest revenue date (date)
        if any(k in text_lower for k in ['ngày nào', 'ngay nao', 'hôm nào']) and (
            any(k in text_lower for k in [
                'doanh thu cao nhất', 'doanh thu nhiều nhất', 'thu về nhiều', 'xét theo doanh thu', 'xet theo doanh thu',
                'bán được nhiều tiền nhất', 'bán đc nhiều tiền nhất', 'ban dc nhieu tien nhat', 'ban duoc nhieu tien nhat',
                'nhiều tiền nhất', 'nhieu tien nhat'
            ]) or (
                any(k in text_lower for k in ['doanh thu']) and any(k in text_lower for k in ['cao nhất', 'cao nhat', 'nhiều nhất', 'đắt nhất', 'dat nhat'])
            )
        ):
            return {
                'intent': 'highest_revenue_date',
                'ans_type': 'date',
                'time_info': time_info
            }

        # 7. Top debt table (table)
        if ('nợ nhiều nhất' in text_lower or 'công nợ lớn nhất' in text_lower) and ('mỗi người' in text_lower or 'số nợ' in text_lower or 'từng người' in text_lower or 'khách' in text_lower):
            m_top = re.search(r'(?:top\s*)?(\d+)\s*khách', text_lower)
            k = int(m_top.group(1)) if m_top else 3
            return {
                'intent': 'top_debt_table',
                'ans_type': 'table',
                'k': k,
                'time_info': time_info
            }

        # 8. Avg import price for specific item (check BEFORE purchase_total)
        m_imp = re.search(r'(.+?)\s+nhập vào giá bình quân\s+(?:một\s+)?(?:[^\s]+\s+)?là bao nhiêu', text_lower)
        if not m_imp:
            m_imp = re.search(r'giá nhập bình quân\s+(?:một\s+)?(?:[^\s]+\s+)?(.+?)\s+là bao nhiêu', text_lower)
        if m_imp:
            item_name = m_imp.group(1).strip()
            # Clean leading time phrases if captured in group(1)
            item_name = re.sub(r'^(?:trong\s+)?(?:tháng|quý|năm)\s+\d+(?:[/-]\d+|\s+năm\s+\d+)?\s*,?\s*', '', item_name).strip()
            return {
                'intent': 'avg_import_price',
                'ans_type': 'number',
                'item_query': item_name,
                'time_info': time_info
            }

        # 9. Purchases / Import total (number)
        if any(k in text_lower for k in ['giá trị các phiếu nhập', 'tiền nhập hàng', 'nhập hàng hết bao nhiêu', 'tổng tiền nhập', 'bỏ ra bao nhiêu tiền nhập hàng']) and not any(
            k in text_lower for k in ['nhà cung cấp', 'ncc', 'mặt hàng', 'sản phẩm', 'nào']
        ):
            return {
                'intent': 'purchase_total',
                'ans_type': 'number',
                'time_info': time_info
            }

        # 10. Order count (số đơn hàng không huỷ)
        if not any(k in text_lower for k in ['huỷ', 'hủy', 'huy', 'khách', 'nhân viên', 'thợ', 'bảo hành', 'trả hàng', 'ghi nợ', 'còn nợ', 'quá hạn', 'giao']) and (
            re.search(r'(?:bao nhiêu|mấy|may|đếm|dem)\s*(?:số\s*)?(?:đơn hàng|đơn|don hang|don|hoá đơn|hoa don|bill|phiếu)', text_lower) or
            re.search(r'(?:có|co|được|duoc)\s+(?:tổng cộng\s+)?(?:mấy|may|bao nhiêu)\s+(?:đơn|don|bill|phiếu)', text_lower) or
            re.search(r'\b(?:số đơn|so don)\b', text_lower)
        ):
            m_min = re.search(r'(?:từ|trên|tu|tren)\s*([\d\.,]+)\s*(?:đồng|dong|đ|k)', text_lower)
            min_rev = 0
            if m_min:
                from datamate.data_loader import clean_currency_str
                min_rev = clean_currency_str(m_min.group(1))
            return {
                'intent': 'order_count',
                'ans_type': 'number',
                'min_revenue': min_rev,
                'time_info': time_info
            }

        # 11. Store Revenue growth % or diff amount (Rulebook §20)
        if (any(k in text_lower for k in [
            'tăng trưởng', 'tang truong', 'tăng bao nhiêu %', 'thay đổi bao nhiêu %', 'thay doi bao nhieu %',
            'cao hay thấp hơn', 'hơn hay kém', 'tăng hay giảm', 'tang hay giam',
            'so với cùng kỳ', 'so voi cung ky', 'so với tháng trước', 'so voi thang truoc', 'so với tháng liền trước',
            'so với năm trước', 'so voi nam truoc', 'so với trước tết', 'so voi truoc tet'
        ]) and any(k in text_lower for k in ['%', 'phần trăm', 'phan tram'])) and not any(k in text_lower for k in [
            'chiếm', 'chiem', 'tỉ trọng', 'tỷ trọng', 'kê đơn', 'ke don', 'bảo hành', 'bao hanh',
            'đơn bị huỷ', 'don bi huy', 'đơn huỷ', 'don huy', 'khách', 'khach',
            'nhóm', 'nhom', 'món', 'mon', 'ca chiều', 'ca sáng', 'ca chieu', 'ca sang',
            'mặt hàng', 'mat hang', 'sản phẩm', 'sp', 'tăng giá', 'số viên', 'số lượng'
        ]):
            return {
                'intent': 'revenue_diff',
                'ans_type': 'percent',
                'is_percent': True,
                'time_info': time_info
            }

        # 12. Active ingredient revenue
        m_act = re.search(r'hoạt chất\s+([^?]+?)(?:\s+tháng|\s+tuần|\s+bán|\s+trong|\?)', text_lower)
        if m_act:
            active_name = m_act.group(1).strip()
            return {
                'intent': 'active_ingredient_rev',
                'ans_type': 'number',
                'active_name': active_name,
                'time_info': time_info
            }

        # 13. Specific Product Revenue:
        # Pattern A: "(mặt hàng|hàng|sản phẩm|món) X thu về / bán được bao nhiêu tiền"
        m_item_rev_a = re.search(r'\b(?:mặt hàng|mat hang|sản phẩm|san pham|món|mon|hàng|hang)\s+(.+?)\s+(?:thu về|thu ve|bán được|ban duoc|bán đc|ban dc|mang về)\s+(?:tổng cộng\s+|tong cong\s+)?(?:bao nhiêu tiền|bao nhieu tien)', text_lower)
        if m_item_rev_a:
            cand_item = m_item_rev_a.group(1).strip()
            if not any(k in cand_item for k in ['quán', 'cửa hàng', 'tiệm', 'toàn bộ', 'tổng', 'nhóm', 'khách']):
                return {
                    'intent': 'item_revenue',
                    'ans_type': 'number',
                    'item_query': cand_item,
                    'time_info': time_info
                }

        # Pattern B: "Doanh thu / tiền bán (của) X trong tháng..."
        m_item_rev_b = re.search(r'(?:doanh thu|tiền bán)\s+(?:của\s+|cua\s+)?(?:mặt hàng\s+|sản phẩm\s+|món\s+)?(.+?)\s+(?:trong\s+|tháng|tuần|hôm|ngày|quý|năm|\?)', text_lower)
        if m_item_rev_b and not any(k in text_lower for k in ['mỗi tháng', 'từng tháng', 'liền nhau', 'mỗi ngày', 'từng ngày', 'nhóm', 'khách', 'điện thoại', 'kênh', 'nhân viên', 'thợ', '%', 'phần trăm', 'tháng nào', 'ngày nào', 'ngay nao', 'quý nào']):
            cand_item = m_item_rev_b.group(1).strip()
            if len(cand_item) >= 3 and not cand_item.isdigit() and not any(k in cand_item for k in ['quán', 'cửa hàng', 'cua hang', 'tiệm', 'toàn bộ', 'tổng', 'ca sáng', 'ca chiều', '3 tháng', '2 tháng', '4 tháng', 'cả ngày', 'ca ngay', 'hôm qua', 'hôm nay']):
                return {
                    'intent': 'item_revenue',
                    'ans_type': 'number',
                    'item_query': cand_item,
                    'time_info': time_info
                }

        # Pattern C: "bán <item_name> được bao nhiêu tiền"
        m_item_rev_c = re.search(r'bán\s+(.+?)\s+(?:được|duoc|đc|dc)\s+(?:tổng cộng\s+|tong cong\s+)?(?:bao nhiêu tiền|bao nhieu tien)', text_lower)
        if m_item_rev_c:
            cand_item = m_item_rev_c.group(1).strip()
            cand_item = re.sub(r'^(?:mặt hàng|sản phẩm|món)\s+', '', cand_item).strip()
            if len(cand_item) >= 4 and not any(k in cand_item for k in ['quán', 'cửa hàng', 'cua hang', 'tiệm', 'hàng', 'trong', 'tháng', 'ngày', 'tuần', 'quý', 'ca ']):
                return {
                    'intent': 'item_revenue',
                    'ans_type': 'number',
                    'item_query': cand_item,
                    'time_info': time_info
                }

        # 14. Item quantity sold:
        # Check specific pattern "mặt hàng X bán được (tổng cộng) bao nhiêu [unit]" FIRST!
        m_qty_2 = re.search(r'(?:mặt hàng|mat hang|sản phẩm|món)\s+(.+?)\s+bán\s*(?:ra|được|duoc|đc|dc)?\s+(?:tổng cộng\s+|tong cong\s+)?(?:bao nhiêu|mấy|may)\s*([^\s\?\.]+)?', text_lower)
        if m_qty_2:
            item_name = m_qty_2.group(1).strip()
            unit_name = m_qty_2.group(2).strip() if m_qty_2.group(2) else None
            return {
                'intent': 'item_qty_sold',
                'ans_type': 'number',
                'item_query': item_name,
                'unit_name': unit_name,
                'time_info': time_info
            }

        # Pattern 1: "bán (ra|được) bao nhiêu <unit> <item_name>"
        m_qty_1 = re.search(r'bán\s+(?:ra|được|duoc|đc|dc)\s+(?:tổng cộng\s+)?(?:bao nhiêu|mấy|may)\s+([^\s\?\.]+)\s+(.+?)[\?\.]?$', text_lower)
        if m_qty_1 and not any(k in text_lower for k in ['size ', 'nhóm ', 'loại ', 'đơn', 'bill', 'phiếu', 'tiền']):
            unit_name = m_qty_1.group(1).strip()
            item_name = m_qty_1.group(2).strip()
            item_name = re.sub(r'\s+(?:vậy|ạ|nhỉ|hả|ha|thế|nhé|đó)$', '', item_name).strip()
            if len(item_name) >= 3 and item_name not in ('vậy', 'ạ', 'nhỉ', 'hả', 'ha', 'thế') and not item_name.startswith(('trong ', 'tháng ', 'tuần ', 'ngày ')):
                return {
                    'intent': 'item_qty_sold',
                    'ans_type': 'number',
                    'item_query': item_name,
                    'unit_name': unit_name,
                    'time_info': time_info
                }

        # 15. Store general revenue total (ONLY if genuinely asking about the entire shop/shift)
        if any(k in text_lower for k in [
            'doanh thu quán', 'doanh thu cửa hàng', 'doanh thu tiệm', 'tiền bán ca', 'doanh thu ca',
            'doanh thu hôm nay', 'doanh thu hôm qua', 'doanh thu năm nay', 'doanh thu trong ngày hôm nay', 'tổng doanh thu trong ngày',
            'doanh thu ngày', 'doanh thu tuần', 'doanh thu tháng', 'doanh thu quý', 'tổng doanh thu quý',
            'doanh thu 30 ngày', 'doanh thu 7 ngày', 'doanh thu 14 ngày', 'doanh thu từ đầu',
            'bán đc tổng cộng bao nhiêu tiền', 'bán được tổng cộng bao nhiêu tiền', 'bán được bao nhiêu tiền', 'bán đc bao nhiêu tiền',
            'thu về bao nhiêu tiền', 'doanh thu là bao nhiêu', 'doanh thu được bao nhiêu',
            'tổng tiền bán', 'tong tien ban', 'doanh thu quan', 'doanh thu cua hang',
            'ban duoc bao nhieu tien', 'thu ve bao nhieu tien', 'ban dc bao nhieu tien',
            'ban duoc tong cong bao nhieu tien', 'doanh thu hom qua', 'doanh thu nam nay',
            'doanh thu ca thang', 'ban duoc may tien'
        ]) and not any(k in text_lower for k in [
            '%', 'phần trăm', 'phan tram', 'so với', 'so voi', 'hơn hay kém', 'hon hay kem',
            'thợ', 'tho', 'khách', 'khach', 'nhân viên', 'nhan vien', 'lãnh', 'lương',
            'huê hồng', 'hoa hong', 'mỗi đơn', 'mổi đơn', 'moi don', 'nhà cung cấp', 'ncc',
            'kê đơn', 'ke don', 'huỷ', 'hủy', 'huy', 'mặt hàng', 'mat hang', 'sản phẩm', 'món',
            'nhóm', 'nhom', 'kênh', 'điện thoại', 'sàn', 'zalo', 'tiền mặt', 'chuyển khoản',
            'mỗi tháng', 'từng tháng', 'liền nhau', 'mỗi ngày', 'từng ngày',
            'tháng nào', 'ngày nào', 'ngay nao', 'quý nào', 'đếm số ngày', 'dem so ngay', 'bao nhiêu ngày', 'mấy ngày'
        ]):
            return {
                'intent': 'revenue_total',
                'ans_type': 'number',
                'time_info': time_info
            }

        # 16. Check if SLM parser is enabled for ambiguous / complex questions
        if self.slm_parser:
            slm_res = self.slm_parser.parse_with_slm(q_text, as_of_date)
            if slm_res and isinstance(slm_res, dict) and slm_res.get('intent'):
                slm_res['time_info'] = time_info
                slm_res['source'] = 'slm'
                return slm_res

        # 17. SAFE SELECTIVE ABSTENTION (RULEBOOK PENALIZED ACCURACY OPTIMIZATION)
        return {
            'intent': 'abstain',
            'ans_type': 'abstain',
            'time_info': time_info
        }

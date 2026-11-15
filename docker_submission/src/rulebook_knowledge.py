import os
import re
from typing import Dict, List, Optional

from .preprocessor import NormalizedQuestion, strip_accents
from .schema_builder import ShopBundle
from .time_resolver import TimeContext


class RulebookRetriever:
    """
    Module Truy xuất Tri thức Nghiệp vụ Động (Rulebook Knowledge Evidence Retriever)
    theo phương pháp BIRD Benchmark (Li et al., NeurIPS 2023) & CHESS (Talaei et al., 2024):
    - Nạp trực tiếp `/data/rulebook.md` (hoặc bản dự phòng đầy đủ 100% quy tắc từ cả Train & Public Test).
    - Trích xuất chính xác các điều khoản Rulebook và công thức toán học tương ứng với câu hỏi & ngành hàng
      để tiêm trực tiếp vào Prompt của Qwen2.5-Coder-14B-Instruct.
    """

    CORE_RULES = """- [Rule §1.1 - Doanh thu & Đơn hợp lệ]: "Doanh thu" / "tiền bán" = tổng giá trị các đơn hàng HỢP LỆ trong kỳ (`is_valid == True`). KHÔNG tính đơn đã huỷ (`is_cancelled == True`, trạng thái "Đã huỷ", "Huỷ", "Cancelled"). KHÔNG tính đơn hoàn/trả hàng (`is_returned == True`, trạng thái "Đã trả hàng", "Hoàn trả", "Returned"). Nếu một đơn chỉ trả một phần thì LOẠI CẢ ĐƠN ĐÓ khỏi doanh thu và khỏi các thống kê bán hàng.
- [Rule §1.2 - Ngày hiện tại (as_of_date)]: Mọi bản ghi có `date > as_of_date` đều là dữ liệu ghi nhầm trong tương lai và BẮT BUỘC phải loại bỏ (`date <= as_of_date`). Hàm `revenue_lines()` và `item_lines()` đã tự động lọc `is_valid == True` và `date <= as_of_date`.
- [Rule §1.3 - Đơn bảo hành]: Đơn có trạng thái "Bảo hành" với thành tiền = 0 không đóng góp vào doanh thu và KHÔNG tính vào mẫu số khi tính giá trị trung bình một đơn (AOV).
- [Rule §4.1 - Quy đổi đơn vị cơ bản]: Khi cộng/so sánh số lượng bán, nhập hoặc tồn của một mặt hàng, PHẢI quy về đơn vị cơ bản nhỏ nhất (`qty_base` trong `V['sales']`, đã nhân hệ số quy đổi từ tên đơn vị như "thùng (24 lon)" = 24 lon, "lốc (6 chai)" = 6 chai, "hộp 10 vỉ x 10 viên" = 100 viên).
- [Rule §4.2 - Làm tròn & Kiểu dữ liệu]:
  + Tiền tệ (VND): Làm tròn đến hàng đơn vị đồng (0 chữ số thập phân, nửa đồng làm tròn lên: `money(x)`).
  + Tỷ lệ phần trăm (%): Làm tròn đến 2 chữ số thập phân (`pct(tu, mau)` hoặc `growth(nay, truoc)`). Ví dụ 12,35% trả về số `12.35` (KHÔNG chia 100 thành 0.1235).
  + Xếp hạng (Tie-break): Khi nhiều mặt hàng/khách hàng/nhà cung cấp có cùng giá trị xếp hạng, BẮT BUỘC ưu tiên mã (`item_code`, `cust_key`, `supplier_id`) nhỏ hơn theo thứ tự chữ cái tăng dần (`kind='mergesort'`), hoặc ngày/giờ sớm hơn."""

    DOMAIN_RULES: Dict[str, str] = {
        "time_shift_weekday": """- [Rule §2.1 - Ca bán hàng]: "Ca sáng" = từ `06:00` đến `13:59` (`6 <= hour <= 13`, `shift='morning'`). "Ca chiều" = từ `14:00` đến `21:59` (`14 <= hour <= 21`, `shift='afternoon'`).
- [Rule §2.2 - Trung bình theo Thứ trong tuần]: "Trung bình mỗi ngày Thứ X (hoặc Chủ nhật) thu bao nhiêu" = Tổng doanh thu của tất cả các ngày Thứ X trong kỳ chia cho TỔNG SỐ NGÀY Thứ X có trong lịch của kỳ đó (kể cả những ngày Thứ X có doanh thu bằng 0, tính đến `min(end, as_of_date)`). Sử dụng hàm `weekday_avg_revenue(iso_weekday)` (Thứ Hai=1 .. Chủ Nhật=7).""",
        "ranking_customer_staff": """- [Rule §3.1 - Bán chạy nhất vs Mang lại nhiều tiền nhất]:
  + "Mặt hàng bán chạy nhất" / "bán nhiều nhất" = Tổng số lượng quy đổi đơn vị cơ bản (`qty_base`) cao nhất trong kỳ (không tính dòng Phí phục vụ): dùng `top_k(item_lines(), value_col='qty_base', k=...)`.
  + "Mặt hàng mang lại doanh thu cao nhất" = Tổng `amount` cao nhất trong kỳ: dùng `top_k(item_lines(), value_col='amount', k=...)`.
  + "Mặt hàng phổ biến nhất" / "ưa chuộng nhất" / "xuất hiện trong nhiều đơn nhất" = Xuất hiện trong nhiều đơn hàng hợp lệ (`order_id.nunique()`) nhất, hòa thì xét tổng `qty_base` cao hơn: dùng `popular_item(k=...)`.
- [Rule §3.2 - Xếp hạng Khách hàng (BỎ QUA 'Khách lẻ')]:
  + BẮT BUỘC loại bỏ `'Khách lẻ'`, `'Khách vãng lai'` (`cust_key == ''`) khi tìm khách hàng cụ thể hoặc đếm khách.
  + "Khách mua nhiều nhất" / "chi tiêu nhiều nhất": Tổng tiền mua hợp lệ (`amount.sum()`) cao nhất: dùng `top_k(df[df['cust_key'] != ''], value_col='amount', key_col='cust_key', name_col='cust_name')`.
  + "Khách trung thành nhất": Số đơn hợp lệ (`order_id.nunique()`) cao nhất, hòa thì xét tổng tiền mua cao hơn: dùng `loyal_customer(k=...)`.
  + "Khách sộp nhất": Chỉ xét khách có từ **3 đơn hợp lệ trở lên** (`orders >= 3`) trong kỳ, chọn người có Giá trị đơn trung bình (`rev / orders`) cao nhất: dùng `big_spender_customer()`.
  + "Khách hay đổi ý nhất": Khách có số đơn bị huỷ (`is_cancelled == True`) nhiều nhất trong kỳ: dùng `fickle_customer()`.
- [Rule §3.3 - Xếp hạng Nhân viên]:
  + "Nhân viên bán tốt nhất": Tổng doanh thu đơn hợp lệ (`amount.sum()`) cao nhất, hòa xét số đơn hợp lệ cao hơn: dùng `best_staff_by_revenue(k=...)`.
  + "Nhân viên chăm chỉ nhất": Số đơn hợp lệ (`order_id.nunique()`) cao nhất, hòa xét tổng doanh thu cao hơn: dùng `hardworking_staff(k=...)`.
- [Rule §3.4 - Ngày đắt khách & Giờ cao điểm]:
  + "Ngày đắt khách nhất" / "đông khách nhất": Ngày có số đơn hợp lệ cao nhất (hòa xét doanh thu cao hơn, rồi ngày sớm hơn): dùng `busiest_date(metric='orders')`.
  + "Giờ cao điểm": Khung giờ nguyên (0..23) có số đơn hợp lệ cao nhất (hòa xét doanh thu cao hơn, rồi giờ nhỏ hơn): dùng `peak_hour()`.""",
        "profit_expense": """- [Rule §5.1 - Lãi gộp (Gross Profit)]:
  + Giá vốn của một mặt hàng chỉ lấy từ cột `cost_price` trong bảng danh mục sản phẩm `V['products']` (KHÔNG tự suy từ phiếu nhập).
  + Lãi gộp 1 dòng bán = `amount - (qty_base * cost_price)`.
  + Tổng lãi gộp trong kỳ = `gross_profit(T['start'], T['end'], T['shift'])`.
  + Mặt hàng lời nhất / lãi gộp cao nhất = `top_gross_profit_item(T['start'], T['end'], k=...)`.
- [Rule §5.2 - Chi phí & Lãi ròng (Net Profit)]:
  + Tổng chi phí trong kỳ = Tổng cột `amount` trong bảng `V['expenses']` có `date` thuộc `[T['start'], min(T['end'], as_of_date)]`: dùng `total_expense()`.
  + Lãi ròng = `Lãi gộp trong kỳ - Tổng chi phí trong kỳ`: dùng `net_profit()`.""",
        "imports_inventory": """- [Rule §5.3 - Nhập hàng & Nhà cung cấp]:
  + Chỉ tính các phiếu nhập hợp lệ (`is_valid == True`, không bị huỷ) có `date <= as_of_date`.
  + Tổng tiền nhập hàng trong kỳ = `total_import_amount()`.
  + "Nhà cung cấp nhập nhiều tiền nhất" = `top_supplier_by_amount()`.
  + "Nhà cung cấp chính" = Nhà cung cấp có số phiếu nhập (`import_id.nunique()`) nhiều nhất trong kỳ, hòa thì xét tổng tiền nhập cao hơn: dùng `main_supplier()`.
- [Rule §5.4 - Tồn kho & Chênh lệch kiểm kê]:
  + Tồn kho lý thuyết cuối ngày D = `opening_stock + Tổng nhập hợp lệ (từ đầu kỳ đến D) - Tổng bán hợp lệ quy đổi (từ đầu kỳ đến D)`.
  + Chênh lệch kiểm kê = `actual_closing_stock (kiểm thực tế) - Tồn kho lý thuyết` (âm là thất thoát/thiếu hụt, dương là thừa).
  + "Hàng ế / không bán được trong kỳ": Mặt hàng đã từng có giao dịch bán trước `T['start']` nhưng có số lượng bán = 0 trong `[T['start'], T['end']]`.""",
        "debt_receivable": """- [Rule §5.5 - Công nợ phải thu]:
  + Chỉ tính công nợ trên các đơn không bị huỷ (`status_norm != 'huy'`) và có `date <= as_of_date`.
  + KHÔNG tính các đơn "Đang giao (COD)" hoặc "Chờ đối soát sàn TMĐT" vào công nợ khách hàng.
  + Dư nợ còn lại = `debt` (hoặc `total_order - paid`).
  + Tuổi nợ của 1 đơn còn nợ tính đến ngày D = `(pd.to_datetime(D) - pd.to_datetime(order_date)).dt.days`. Phân nhóm tuổi nợ: Trong hạn (`<= 30` ngày), Quá hạn nhẹ (`31..60` ngày), Nợ xấu/quá hạn nặng (`> 60` ngày).""",
        "payment_channel_group": """- [Rule §1.4 - Phương thức thanh toán, Kênh bán & Nhóm khách]:
  + Phương thức thanh toán (`payment_method`): Dùng `revenue_by_payment('tiền mặt' | 'chuyển khoản' | 'thẻ' | 'ví điện tử' | 'cod')`.
  + Kênh bán hàng (`channel`): Dùng `revenue_by_channel('tại quầy' | 'online' | 'shopee' | 'grab' | 'tiktok' | 'zalo' | 'web')`.
  + Nhóm khách hàng (`cust_group` trong `V['customers']`): Dùng `revenue_by_cust_group('VIP' | 'Thân thiết' | 'Thường', metric='revenue' | 'orders' | 'customers')`.""",
        "domain_specific": """- [Rule §6 - Quy tắc riêng theo Ngành hàng]:
  + **Nhà thuốc (Pharmacy)**:
    * Thuốc kê đơn (`is_prescription == True` trong `V['products']`) vs Thuốc không kê đơn (`is_prescription == False`).
    * Hoạt chất (`active_ingredient` trong `V['products']`): Gộp tất cả các thuốc có cùng hoạt chất chính (không phân biệt hoa/thường).
    * Lô thuốc cận hạn / hết hạn tính đến ngày D: Chỉ xét các lô còn tồn (`closing_stock > 0`), hết hạn khi `expiry_date < D`, cận hạn trong N ngày khi `D <= expiry_date <= D + N ngày`.
  + **Quán cà phê & F&B**:
    * "Phí phục vụ" / "Phụ thu phòng lạnh" (`is_service_fee == True`): ĐƯỢC cộng vào tổng doanh thu cửa hàng (`revenue_lines()`), nhưng KHÔNG được coi là một món hàng khi xếp hạng hay đếm số lượng món bán ra (`item_lines()`).
    * Size đồ uống `(M)`, `(L)`: Khi hỏi riêng số ly size M hoặc size L thì dùng `qty_by_coffee_size('M')` / `qty_by_coffee_size('L')`. Khi hỏi món nào bán chạy nhất thì gộp mọi size của cùng một món.
  + **Tiệm sửa xe & Phụ tùng**:
    * Tiền công thợ (`labor_fee` / nhóm `'Công sửa chữa'` / `'Dịch vụ'`) vs Tiền phụ tùng (`parts_fee` / phụ tùng thay thế).
    * Đơn có trạng thái `"Bảo hành"` với thành tiền = 0 không tính vào doanh thu và không tính vào mẫu số khi tính trung bình mỗi phiếu sửa.
    * Doanh thu / số phiếu của thợ kỹ thuật: lọc theo cột `staff` (hoặc `technician`).
  + **Cửa hàng Điện thoại & Phụ kiện**:
    * Đơn trả góp: Doanh thu vẫn tính theo tổng giá trị máy bán ra (`amount` / `total_order`), phần công ty tài chính chưa giải ngân tính vào khoản phải thu trả góp, không nhầm với nợ khách lẻ.
    * Thu cũ đổi mới: Doanh thu bán máy mới tính theo giá niêm yết sau khuyến mãi; giá trị thu máy cũ là phương thức thanh toán bù trừ, không trừ vào doanh thu bán hàng.
  + **Shop Mẹ & Bé**:
    * Chương trình tích điểm / đổi điểm và Voucher: Doanh thu thuần tính theo số tiền thực thu sau khi trừ chiết khấu/voucher/điểm quy đổi (`amount`).""",
    }

    def __init__(self, rulebook_path: Optional[str] = None) -> None:
        self.raw_rulebook_text: str = ""
        if rulebook_path and os.path.exists(rulebook_path):
            try:
                with open(rulebook_path, "r", encoding="utf-8") as f:
                    self.raw_rulebook_text = f.read().strip()
            except Exception:
                self.raw_rulebook_text = ""

    def retrieve_evidence(
        self,
        nq: NormalizedQuestion,
        bundle: ShopBundle,
        tctx: TimeContext,
        spec_type: str,
    ) -> str:
        """
        Chọn lọc các mục Rulebook sát nhất với câu hỏi và đặc trưng cửa hàng.
        Đảm bảo SLM luôn có đủ công thức chuẩn mà không bị nhiễu ngữ cảnh.
        """
        q_a = nq.ascii
        caps = bundle.profile.capabilities
        sections: List[str] = [self.CORE_RULES]

        # 1. Thời gian / Ca bán / Thứ trong tuần
        if (
            tctx.shift
            or tctx.compare_shift
            or any(
                k in q_a
                for k in [
                    "ca sang",
                    "ca chieu",
                    "ca toi",
                    "thu hai",
                    "thu ba",
                    "thu tu",
                    "thu nam",
                    "thu sau",
                    "thu bay",
                    "chu nhat",
                    "moi ngay thu",
                    "cuoi tuan",
                ]
            )
        ):
            sections.append(self.DOMAIN_RULES["time_shift_weekday"])

        # 2. Xếp hạng / Khách hàng / Nhân viên / Giờ cao điểm
        if (
            spec_type in ("string", "list", "table", "date")
            or any(
                k in q_a
                for k in [
                    "nhat",
                    "top",
                    "khach",
                    "nhan vien",
                    "tho",
                    "gio cao diem",
                    "khung gio",
                    "pho bien",
                    "ua chuong",
                    "trung thanh",
                    "khach sop",
                    "doi y",
                    "cham chi",
                    "dat khach",
                    "dong khach",
                ]
            )
        ):
            sections.append(self.DOMAIN_RULES["ranking_customer_staff"])

        # 3. Lãi gộp / Lãi ròng / Chi phí
        if any(
            k in q_a
            for k in [
                "lai gop",
                "lai rong",
                "loi nhuan",
                "loi nhat",
                "gia von",
                "chi phi",
                "tien dien",
                "tien thue",
                "mat bang",
                "luong",
            ]
        ):
            sections.append(self.DOMAIN_RULES["profit_expense"])

        # 4. Nhập hàng / Nhà cung cấp / Tồn kho / Hàng ế
        if any(
            k in q_a
            for k in [
                "nhap",
                "nha cung cap",
                "ton kho",
                "trong kho",
                "kiem ke",
                "that thoat",
                "chenh lech",
                "hang e",
                "khong ban duoc",
                "het hang",
            ]
        ):
            sections.append(self.DOMAIN_RULES["imports_inventory"])

        # 5. Công nợ / Tuổi nợ / Trả nợ
        if any(
            k in q_a
            for k in [
                "no",
                "cong no",
                "con thieu",
                "qua han",
                "tuoi no",
                "thu hoi",
            ]
        ):
            sections.append(self.DOMAIN_RULES["debt_receivable"])

        # 6. Phương thức thanh toán / Kênh bán / Nhóm khách
        if any(
            k in q_a
            for k in [
                "tien mat",
                "chuyen khoan",
                "vi dien tu",
                "momo",
                "vnpay",
                "zalopay",
                "cod",
                "the",
                "kenh",
                "tai quay",
                "online",
                "shopee",
                "grab",
                "tiktok",
                "zalo",
                "web",
                "vip",
                "than thiet",
                "nhom khach",
            ]
        ):
            sections.append(self.DOMAIN_RULES["payment_channel_group"])

        # 7. Ngành hàng chuyên biệt (Nhà thuốc, Cà phê, Sửa xe, Điện thoại, Mẹ & Bé)
        if (
            caps.get("has_rx")
            or caps.get("has_active")
            or caps.get("has_lot_expiry")
            or any(
                k in q_a
                for k in [
                    "ke don",
                    "hoat chat",
                    "han su dung",
                    "het han",
                    "can han",
                    "so lo",
                    "ma lo",
                    "size",
                    "phong lanh",
                    "phi phuc vu",
                    "phu thu",
                    "sua xe",
                    "phu tung",
                    "cong tho",
                    "bao hanh",
                    "bien so",
                    "dong xe",
                    "tra gop",
                    "thu cu",
                    "may cu",
                    "imei",
                    "tich diem",
                    "dung diem",
                    "voucher",
                    "sua so",
                    "bim",
                    "ta",
                ]
            )
        ):
            sections.append(self.DOMAIN_RULES["domain_specific"])

        return "\n".join(sections)


class DynamicFewShotSelector:
    """
    Bộ chọn Few-Shot Exemplars động theo phương pháp DAIL-SQL (Gao et al., VLDB 2024):
    Cung cấp các đoạn code mẫu chuẩn xác sử dụng các hàm Atomic Helpers trong Sandbox
    tương ứng với nhóm câu hỏi đang xét.
    """

    EXEMPLARS: Dict[str, str] = {
        "profit_expense": """# Ví dụ 1 (Tính lãi gộp trong kỳ):
final_result = gross_profit(T['start'], T['end'], T['shift'])

# Ví dụ 2 (Tìm mặt hàng mang lại lãi gộp cao nhất / lời nhất):
final_result = top_gross_profit_item(T['start'], T['end'], k=1)

# Ví dụ 3 (Tính lãi ròng = Lãi gộp - Tổng chi phí trong kỳ):
final_result = net_profit(T['start'], T['end'])""",
        "customer_staff_ranking": """# Ví dụ 1 (Khách sộp nhất: >= 3 đơn hợp lệ, AOV cao nhất):
final_result = big_spender_customer(T['start'], T['end'])

# Ví dụ 2 (Khách trung thành nhất / Nhân viên chăm chỉ nhất / Nhân viên doanh thu cao nhất):
final_result = loyal_customer(T['start'], T['end'], k=1)
# Hoặc cho nhân viên:
# final_result = hardworking_staff(T['start'], T['end'], k=1)
# final_result = best_staff_by_revenue(T['start'], T['end'], k=1)

# Ví dụ 3 (Mặt hàng phổ biến / xuất hiện trong nhiều đơn nhất):
final_result = popular_item(T['start'], T['end'], k=1)""",
        "time_peak_weekday": """# Ví dụ 1 (Giờ cao điểm trong kỳ - trả về số nguyên 0..23):
final_result = peak_hour(T['start'], T['end'])

# Ví dụ 2 (Ngày đắt khách nhất / nhiều đơn nhất trong kỳ):
final_result = busiest_date(T['start'], T['end'], metric='orders')

# Ví dụ 3 (Doanh thu trung bình mỗi ngày Thứ Bảy [iso_weekday=6] hoặc Chủ Nhật [iso_weekday=7] trong kỳ):
final_result = weekday_avg_revenue(7, T['start'], T['end'])""",
        "payment_channel_supplier": """# Ví dụ 1 (Doanh thu thanh toán bằng chuyển khoản / tiền mặt / ví điện tử):
final_result = revenue_by_payment('chuyển khoản', T['start'], T['end'])

# Ví dụ 2 (Doanh thu bán qua kênh Online / Tại quầy / Shopee / Grab):
final_result = revenue_by_channel('online', T['start'], T['end'])

# Ví dụ 3 (Nhà cung cấp chính [nhiều phiếu nhập nhất] vs Nhà cung cấp nhập nhiều tiền nhất):
final_result = main_supplier(T['start'], T['end'])
# Hoặc: final_result = top_supplier_by_amount(T['start'], T['end'])""",
        "general_sales": """# Ví dụ 1 (Truy vấn trên dòng bán hợp lệ `item_lines()` và lọc mặt hàng `match_item`):
df = match_item(item_lines(T['start'], T['end'], T['shift']), E['item'][0]['name'])
final_result = money(df['amount'].sum()) if not df.empty else None

# Ví dụ 2 (Tính tỷ trọng % doanh thu của một tập con so với tổng doanh thu):
df = revenue_lines(T['start'], T['end'])
tot = float(df['amount'].sum()) if not df.empty else 0.0
sub_rev = float(df[df['item_group'] == 'Đồ uống']['amount'].sum()) if not df.empty else 0.0
final_result = pct(sub_rev, tot) if tot > 0 else None""",
    }

    @classmethod
    def select(cls, nq: NormalizedQuestion, spec_type: str) -> str:
        q_a = nq.ascii
        chosen: List[str] = []
        if any(
            k in q_a
            for k in ["lai gop", "lai rong", "loi nhuan", "loi nhat", "chi phi", "gia von"]
        ):
            chosen.append(cls.EXEMPLARS["profit_expense"])
        if any(
            k in q_a
            for k in [
                "khach",
                "nhan vien",
                "tho",
                "pho bien",
                "ua chuong",
                "trung thanh",
                "khach sop",
                "doi y",
                "cham chi",
            ]
        ):
            chosen.append(cls.EXEMPLARS["customer_staff_ranking"])
        if any(
            k in q_a
            for k in [
                "gio cao diem",
                "khung gio",
                "dat khach",
                "dong khach",
                "moi ngay thu",
                "chu nhat",
                "thu bay",
            ]
        ):
            chosen.append(cls.EXEMPLARS["time_peak_weekday"])
        if any(
            k in q_a
            for k in [
                "tien mat",
                "chuyen khoan",
                "vi dien tu",
                "cod",
                "kenh",
                "tai quay",
                "online",
                "nha cung cap",
                "phieu nhap",
            ]
        ):
            chosen.append(cls.EXEMPLARS["payment_channel_supplier"])
        if not chosen:
            chosen.append(cls.EXEMPLARS["general_sales"])
        return "\n\n".join(chosen[:2])

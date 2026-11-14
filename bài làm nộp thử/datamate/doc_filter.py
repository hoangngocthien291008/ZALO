import re

class DocumentSchemaFilter:
    """
    "Đọc Báo Lọc Báo" (Document & Schema Retrieval + Multi-Stage Filtering Gate).
    Reads each business's README.md, table schemas, and date coverage before allowing
    either Heuristic or 14B QLoRA SLM to execute a query.
    
    3-Stage Filtering Architecture:
    - Stage 1 (README & Date Coverage Filter): Detects unanswerable queries per Rulebook §7
      (e.g. queried period completely outside shop's [min_date, max_date], or unrecorded info).
    - Stage 2 (Schema Capability Filter): Verifies the shop's normalized tables actually contain
      the columns/records needed for the predicted intent.
    - Stage 3 (SLM Anti-Hallucination & Confidence Gate): Prevents the 14B SLM from mapping
      unsupported metrics (net profit, inventory, mechanic commission, loyalty points, etc.)
      onto basic intents like revenue_total or best_sellers, eliminating -1 penalties.
    """

    UNANSWERABLE_KEYWORDS = [
        'nhà ở đường', 'địa chỉ nhà', 'quận mấy', 'phường nào', 'cách bao xa',
        'dự đoán', 'dự báo', 'tháng sau', 'ngày mai', 'tuần sau', 'năm sau',
        'tương lai', 'thời tiết', 'mấy tuổi', 'sinh năm', 'ngày sinh', 'email'
    ]

    # Metrics that require specialized executors not covered by basic revenue/order/bestseller intents
    UNSUPPORTED_METRIC_PATTERNS = [
        r'\blãi ròng\b', r'\blãi gộp\b', r'\blợi nhuận\b', r'\blời nhất\b', r'\bgiá vốn\b',
        r'\btồn kho\b', r'\bcòn tồn\b', r'\bhết hàng\b', r'\bhạn sử dụng\b', r'\bhết hạn\b', r'\bsố lô\b', r'\bmã lô\b',
        r'\bnhân viên\b', r'\bthợ\b', r'\bhoa hồng\b', r'\bchăm chỉ\b', r'\bđứng ca\b',
        r'\bkhách trung thành\b', r'\bkhách sộp\b', r'\bkhách quen\b', r'\bhay đổi ý\b',
        r'\bnhóm khách\b', r'\bkhách thành viên\b', r'\bthành viên mới\b', r'\bđăng ký\b',
        r'\bdùng điểm\b', r'\btích điểm\b', r'\bvoucher\b', r'\bphí ship\b', r'\bphí giao\b',
        r'\bgiờ cao điểm\b', r'\bkhung giờ\b', r'\bđắt khách nhất\b',
        r'\bbàn số\b', r'\bbàn nào\b', r'\bkhu vực\b', r'\bphòng lạnh\b', r'\bphí phục vụ\b',
        r'\bđợt giao\b', r'\bchở giao\b', r'\bbiển số\b', r'\bdòng xe\b', r'\bchiếc xe\b',
        r'\bbảo hành\b', r'\btrả góp\b', r'\bquá hạn\b', r'\bthu máy cũ\b', r'\bdòng máy\b',
        r'\bsize\b', r'\bsữa số\b', r'\bưa chuộng nhất\b',
        r'\btăng giá\b', r'\bđổi giá\b', r'\bđơn giá bao nhiêu\b', r'\bgiá bán một\b', r'\bgiá bán 1\b',
        r'\bnhà cung cấp\b', r'\bncc\b', r'\bnhập trừ bán\b', r'\bchênh nhập\b',
        r'\btuổi nợ\b', r'\bnợ thêm\b', r'\bthu nợ\b', r'\btrả nợ\b', r'\bcòn nợ tổng cộng\b', r'\bbao nhiêu khách còn nợ\b',
        r'\btiền mặt\b', r'\bchuyển khoản\b', r'\bví điện tử\b', r'\bcod\b', r'\bkênh\b', r'\bsàn tmđt\b', r'\bzalo\b',
        r'\btrung bình mỗi ngày thứ\b', r'\bchiếm bao nhiêu %\b', r'\btỉ trọng\b', r'\btỷ trọng\b'
    ]

    @classmethod
    def extract_shop_profile(cls, adapter):
        """
        Builds a compact summary of the shop's README, schema capabilities, and date coverage
        for both deterministic filtering and SLM prompt context ("Đọc báo").
        """
        loader = adapter.loader
        min_date = None
        max_date = loader.max_data_date

        if adapter.items_df is not None and not adapter.items_df.empty and 'date' in adapter.items_df.columns:
            valid_dates = [d for d in adapter.items_df['date'].dropna().unique() if isinstance(d, str) and len(d) == 10 and d.startswith('202')]
            if valid_dates:
                min_date = min(valid_dates)
                if not max_date:
                    max_date = max(valid_dates)
        elif adapter.orders_df is not None and not adapter.orders_df.empty and 'date' in adapter.orders_df.columns:
            valid_dates = [d for d in adapter.orders_df['date'].dropna().unique() if isinstance(d, str) and len(d) == 10 and d.startswith('202')]
            if valid_dates:
                min_date = min(valid_dates)
                if not max_date:
                    max_date = max(valid_dates)

        has_items = adapter.items_df is not None and not adapter.items_df.empty and not getattr(adapter, 'is_order_summary_only', False)
        has_orders = (adapter.orders_df is not None and not adapter.orders_df.empty) or (adapter.items_df is not None and not adapter.items_df.empty)
        has_imports = adapter.imports_df is not None and not adapter.imports_df.empty
        has_rx = adapter.products_df is not None and 'is_prescription' in adapter.products_df.columns
        has_active = adapter.products_df is not None and 'active_ingredient' in adapter.products_df.columns
        has_debt = adapter.orders_df is not None and 'debt' in adapter.orders_df.columns and (adapter.orders_df['debt'] > 0).any()

        return {
            'min_date': min_date,
            'max_date': max_date,
            'has_items': has_items,
            'has_orders': has_orders,
            'has_imports': has_imports,
            'has_rx': has_rx,
            'has_active': has_active,
            'has_debt': has_debt,
            'files': list(loader.data_files.keys())
        }

    @classmethod
    def validate_and_filter(cls, question_text, as_of_date, parsed_result, adapter):
        """
        Applies the 3-stage "Đọc báo lọc báo" verification gate.
        Returns the validated/adjusted parsed_result dict.
        """
        if not parsed_result or not isinstance(parsed_result, dict):
            return {'intent': 'abstain', 'ans_type': 'abstain'}

        intent = parsed_result.get('intent', 'abstain')
        if intent in ('abstain', 'unanswerable'):
            return parsed_result

        q_l = question_text.lower().strip()
        profile = cls.extract_shop_profile(adapter)
        time_info = parsed_result.get('time_info') or {}

        # Stage 1: Check Unanswerable Keywords
        if any(k in q_l for k in cls.UNANSWERABLE_KEYWORDS if k != 'tháng sau') or ('tháng sau' in q_l and 'tháng trước' not in q_l and 'luôn cao hơn' not in q_l):
            return {
                'intent': 'unanswerable',
                'ans_type': 'unanswerable',
                'reason': 'Dữ liệu cửa hàng không lưu thông tin này hoặc yêu cầu dự đoán tương lai.'
            }

        # Stage 1b: Check Date Coverage against shop's [min_date, max_date]
        # Rulebook §7: If queried period is completely outside shop's data range -> unanswerable
        start_d = time_info.get('start_date')
        end_d = time_info.get('end_date')
        if start_d and end_d and profile['min_date'] and profile['max_date']:
            if end_d < profile['min_date'] or start_d > profile['max_date']:
                # If completely outside data range, abstain or unanswerable
                # Note: abstaining is 0 pts, unanswerable is +1 if truly out of range or -1 if date parser misparsed.
                # To be 100% safe against -1 penalty, if period is outside range we abstain unless explicitly asking future year.
                return {'intent': 'abstain', 'ans_type': 'abstain'}

        # Stage 2: Schema Capability Filter
        if intent in ('best_sellers', 'item_qty_sold', 'item_revenue', 'item_unit_price', 'unsold_items', 'unsold_items_count') and not profile['has_items']:
            return {'intent': 'abstain', 'ans_type': 'abstain'}

        if intent in ('purchase_total', 'avg_import_price') and not profile['has_imports']:
            return {'intent': 'abstain', 'ans_type': 'abstain'}

        if intent in ('prescription_rate', 'prescription_rev') and not profile['has_rx']:
            return {'intent': 'abstain', 'ans_type': 'abstain'}

        if intent == 'active_ingredient_rev' and not profile['has_active']:
            return {'intent': 'abstain', 'ans_type': 'abstain'}

        # Stage 3: Anti-Hallucination & Unsupported Metric Gate
        # Except for prescription_rate/rev and active_ingredient_rev which specifically handle medicine
        if intent in ('revenue_total', 'order_count', 'aov', 'best_sellers', 'item_revenue', 'item_qty_sold', 'revenue_diff', 'highest_revenue_date'):
            for pat in cls.UNSUPPORTED_METRIC_PATTERNS:
                if re.search(pat, q_l):
                    # Allow 'chiếm bao nhiêu %' only for prescription_rate / cancellation_rate
                    return {'intent': 'abstain', 'ans_type': 'abstain'}

        # Extra verification if parsed by SLM (source == 'slm')
        if parsed_result.get('source') == 'slm':
            # Verify SLM's predicted intent has required lexical anchor in the question
            if intent == 'revenue_total':
                if not any(k in q_l for k in ['doanh thu', 'dthu', 'bao nhiêu tiền', 'bnhiu tiền', 'mấy tiền', 'tổng tiền bán', 'thu về']):
                    return {'intent': 'abstain', 'ans_type': 'abstain'}
                # Must not be asking about a specific item or person or multiple months list
                if any(k in q_l for k in ['mỗi tháng', 'từng tháng', 'mỗi ngày', 'từng ngày', 'mặt hàng', 'sản phẩm', 'món']):
                    return {'intent': 'abstain', 'ans_type': 'abstain'}
            elif intent == 'aov':
                if not any(k in q_l for k in ['trung bình', 'bình quân']):
                    return {'intent': 'abstain', 'ans_type': 'abstain'}
            elif intent == 'order_count':
                if not any(k in q_l for k in ['đơn', 'bill', 'hóa đơn', 'hoá đơn', 'phiếu']):
                    return {'intent': 'abstain', 'ans_type': 'abstain'}
            elif intent == 'cancellation_rate':
                if not any(k in q_l for k in ['huỷ', 'hủy', 'huy']) or not any(k in q_l for k in ['%', 'phần trăm', 'tỉ lệ', 'tỷ lệ', 'ti le', 'ty le']):
                    return {'intent': 'abstain', 'ans_type': 'abstain'}
            elif intent == 'cancelled_order_count':
                if not any(k in q_l for k in ['huỷ', 'hủy', 'huy']):
                    return {'intent': 'abstain', 'ans_type': 'abstain'}
            elif intent == 'best_sellers':
                if not any(k in q_l for k in ['bán chạy', 'chạy nhất', 'nhiều nhất', 'hot nhất']):
                    return {'intent': 'abstain', 'ans_type': 'abstain'}

        return parsed_result

import re
import pandas as pd
from datamate.formatters import format_number, format_percent, format_date, format_boolean, format_list, format_table

def is_cancelled(status_str):
    s = str(status_str).lower().strip()
    if any(k in s for k in ['huỷ', 'hủy', 'cancel']):
        return True
    if re.search(r'\bhuy\b', s):
        return True
    return False

def is_returned(status_str):
    s = str(status_str).lower().strip()
    if 'hoàn thành' in s or 'hoan thanh' in s or 'thành công' in s:
        return False
    return any(k in s for k in [
        'trả hàng', 'đã trả', 'tra hang', 'hàng trả', 'hang tra', 'trả về', 'tra ve',
        'hoàn hàng', 'hoan hang', 'hoàn trả', 'hoan tra', 'đổi trả', 'doi tra',
        'trả lại', 'tra lai', 'khách trả', 'return'
    ])

def is_warranty(status_str):
    s = str(status_str).lower().strip()
    return 'bảo hành' in s or 'bao hanh' in s

def normalize_query_str(s):
    if not s:
        return ''
    return re.sub(r'\s+', ' ', str(s).lower()).strip()

def filter_items_in_period(items_df, start_date, end_date, as_of_date=None, shift=None):
    if items_df is None or items_df.empty:
        return pd.DataFrame()

    df = items_df.copy()
    
    # Exclude records after as_of_date (Rulebook §5)
    if as_of_date and 'date' in df.columns:
        df = df[df['date'] <= as_of_date]

    # Filter period
    if start_date and 'date' in df.columns:
        df = df[df['date'] >= start_date]
    if end_date and 'date' in df.columns:
        df = df[df['date'] <= end_date]

    # Shift filter (Rulebook §37)
    # ca sáng 06:00–13:59, ca chiều 14:00–21:59
    if shift and 'raw_date' in df.columns:
        def match_shift(val):
            s = str(val)
            m = re.search(r'(\d{1,2}):(\d{2})', s)
            if not m:
                return True
            hour = int(m.group(1))
            if shift == 'morning':
                return 6 <= hour <= 13
            elif shift == 'afternoon':
                return 14 <= hour <= 21
            return True
        df = df[df['raw_date'].apply(match_shift)]

    # Rulebook §1: Exclude cancelled orders and returned orders completely
    if 'status' in df.columns:
        if 'order_id' in df.columns:
            bad_orders = set(df[df['status'].apply(lambda s: is_cancelled(s) or is_returned(s))]['order_id'].dropna().unique())
            df = df[~df['order_id'].isin(bad_orders)]
        else:
            df = df[~df['status'].apply(lambda s: is_cancelled(s) or is_returned(s))]

    return df

def strip_accents(s):
    import unicodedata
    s = str(s).replace('đ', 'd').replace('Đ', 'D')
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')

def match_item_rows(df, item_query):
    """
    Matches product rows in df against item_query with whitespace and accent normalization.
    """
    if df is None or df.empty or not item_query:
        return pd.DataFrame()
    q_l = normalize_query_str(item_query)
    if not q_l:
        return pd.DataFrame()

    norm_names = df['item_name'].apply(normalize_query_str)
    # 1. Exact match first
    exact = df[norm_names == q_l]
    if not exact.empty:
        return exact

    # 2. Substring match
    sub = df[norm_names.str.contains(re.escape(q_l), regex=True)]
    if not sub.empty:
        return sub

    # 3. Token match (all significant tokens)
    words = [w for w in q_l.split() if len(w) > 1]
    if len(words) >= 2:
        def has_all(s):
            return all(w in s for w in words)
        tok_match = df[norm_names.apply(has_all)]
        if not tok_match.empty:
            return tok_match

    # 4. Accent-insensitive match (for unaccented Vietnamese queries like 'nuoc ep dua hau (l)')
    q_no_acc = strip_accents(q_l)
    names_no_acc = norm_names.apply(strip_accents)
    exact_na = df[names_no_acc == q_no_acc]
    if not exact_na.empty:
        return exact_na
    sub_na = df[names_no_acc.str.contains(re.escape(q_no_acc), regex=True)]
    if not sub_na.empty:
        return sub_na

    return pd.DataFrame()

class QueryExecutors:
    @staticmethod
    def calc_revenue(adapter, time_info, as_of_date):
        start_d = time_info['start_date']
        end_d = time_info['end_date']
        shift = time_info.get('shift')

        valid_items = filter_items_in_period(adapter.items_df, start_d, end_d, as_of_date, shift)
        if valid_items.empty:
            return None
        total_rev = valid_items['amount'].sum()
        if total_rev == 0:
            return None
        return format_number(total_rev, is_currency=True)

    @staticmethod
    def calc_aov(adapter, time_info, as_of_date):
        start_d = time_info['start_date']
        end_d = time_info['end_date']
        shift = time_info.get('shift')

        valid_items = filter_items_in_period(adapter.items_df, start_d, end_d, as_of_date, shift)
        if valid_items.empty:
            return None
        # Rulebook §115: Tiền trung bình một phiếu chia cho số phiếu có thu tiền (không tính phiếu bảo hành tiền 0)
        if 'status' in valid_items.columns:
            valid_items = valid_items[~(valid_items['status'].apply(is_warranty) & (valid_items['amount'] == 0))]
        if valid_items.empty:
            return None
        total_rev = valid_items['amount'].sum()
        order_count = len(valid_items['order_id'].unique())
        if order_count == 0 or total_rev == 0:
            return None
        # Rulebook §50 & §167: AOV làm tròn về đồng, nửa đồng làm tròn lên
        aov = int( (total_rev / order_count) + 0.5 )
        return aov

    @staticmethod
    def calc_revenue_diff(adapter, time_info, as_of_date, is_percent=False):
        start_d = time_info['start_date']
        end_d = time_info['end_date']
        cmp_start = time_info.get('compare_start_date')
        cmp_end = time_info.get('compare_end_date')

        if not cmp_start or not cmp_end:
            return None

        curr_items = filter_items_in_period(adapter.items_df, start_d, end_d, as_of_date)
        prev_items = filter_items_in_period(adapter.items_df, cmp_start, cmp_end, as_of_date)

        rev_curr = curr_items['amount'].sum() if not curr_items.empty else 0.0
        rev_prev = prev_items['amount'].sum() if not prev_items.empty else 0.0

        if is_percent:
            if rev_prev == 0 or rev_curr == 0:
                return None
            pct = (rev_curr - rev_prev) / rev_prev * 100.0
            return format_percent(pct)
        else:
            if rev_prev == 0 and rev_curr == 0:
                return None
            diff = rev_curr - rev_prev
            return format_number(diff, is_currency=True)

    @staticmethod
    def calc_cancellation_rate(adapter, time_info, as_of_date):
        start_d = time_info['start_date']
        end_d = time_info['end_date']

        df = adapter.orders_df
        if df is None or df.empty:
            df = adapter.items_df[['order_id', 'date', 'status']].drop_duplicates()

        if df is None or df.empty:
            return None

        sub = df[(df['date'] >= start_d) & (df['date'] <= end_d)]
        if as_of_date:
            sub = sub[sub['date'] <= as_of_date]

        if sub.empty:
            return None

        total_orders = len(sub['order_id'].unique())
        cancelled_orders = len(sub[sub['status'].apply(is_cancelled)]['order_id'].unique())

        if total_orders == 0:
            return None
        rate = (cancelled_orders / total_orders) * 100.0
        return format_percent(rate)

    @staticmethod
    def calc_cancelled_order_count(adapter, time_info, as_of_date):
        start_d = time_info['start_date']
        end_d = time_info['end_date']

        df = adapter.orders_df
        if df is None or df.empty:
            df = adapter.items_df[['order_id', 'date', 'status']].drop_duplicates()

        if df is None or df.empty:
            return 0

        sub = df[(df['date'] >= start_d) & (df['date'] <= end_d)]
        if as_of_date:
            sub = sub[sub['date'] <= as_of_date]

        if sub.empty:
            return 0

        cancelled_orders = len(sub[sub['status'].apply(is_cancelled)]['order_id'].unique())
        return int(cancelled_orders)

    @staticmethod
    def calc_order_count(adapter, time_info, as_of_date, min_revenue=0):
        start_d = time_info['start_date']
        end_d = time_info['end_date']
        shift = time_info.get('shift')

        valid_items = filter_items_in_period(adapter.items_df, start_d, end_d, as_of_date, shift)
        if valid_items.empty:
            return 0

        order_rev = valid_items.groupby('order_id')['amount'].sum()
        if min_revenue > 0:
            order_rev = order_rev[order_rev >= min_revenue]

        return int(len(order_rev))

    @staticmethod
    def calc_best_sellers(adapter, time_info, as_of_date, k=1, item_group=None):
        start_d = time_info['start_date']
        end_d = time_info['end_date']

        valid_items = filter_items_in_period(adapter.items_df, start_d, end_d, as_of_date)
        if valid_items.empty:
            return None

        # Filter out service fees (Rulebook §43)
        valid_items = valid_items[~valid_items['item_name'].str.lower().str.contains('phí phục vụ')]

        if item_group:
            grp_l = item_group.lower().strip()
            matched = False
            if 'item_group' in valid_items.columns:
                sub = valid_items[valid_items['item_group'].str.lower().str.contains(re.escape(grp_l))]
                if not sub.empty:
                    valid_items = sub
                    matched = True
            if not matched:
                sub_name = valid_items[valid_items['item_name'].str.lower().str.contains(re.escape(grp_l))]
                if not sub_name.empty:
                    valid_items = sub_name

        # Rulebook §45: by base quantity (qty_base), ties ordered by item_code ascending
        grouped = valid_items.groupby(['item_code', 'item_name'])['qty_base'].sum().reset_index()
        grouped = grouped.sort_values(by=['qty_base', 'item_code'], ascending=[False, True])

        if grouped.empty:
            return None

        top_names = grouped['item_name'].tolist()
        clean_names = []
        for n in top_names:
            n_str = str(n).strip()
            nl = n_str.lower()
            if nl in ['khách lẻ', 'khach le', 'nan', 'none', '', 'null', 'tong cong', 'tổng cộng', 'đơn hàng']:
                continue
            if nl.startswith(('ord', 'hd_', 'hđ_')):
                continue
            clean_names.append(n_str)
            if len(clean_names) == k:
                break

        if not clean_names:
            return None
        if k == 1:
            return clean_names[0]
        return clean_names

    @staticmethod
    def calc_highest_revenue_date(adapter, time_info, as_of_date):
        start_d = time_info['start_date']
        end_d = time_info['end_date']

        valid_items = filter_items_in_period(adapter.items_df, start_d, end_d, as_of_date)
        if valid_items.empty:
            return None

        daily = valid_items[valid_items['date'] != ''].groupby('date')['amount'].sum().reset_index()
        daily = daily.sort_values(by=['amount', 'date'], ascending=[False, True])
        if daily.empty:
            return None
        return format_date(daily.iloc[0]['date'])

    @staticmethod
    def calc_continuous_increase(adapter, periods, as_of_date):
        if not periods or len(periods) < 2:
            return None
        revs = []
        for p in periods:
            items = filter_items_in_period(adapter.items_df, p['start_date'], p['end_date'], as_of_date)
            rev = items['amount'].sum() if not items.empty else 0.0
            revs.append(rev)

        if any(r == 0 for r in revs):
            return None

        for i in range(len(revs) - 1):
            if revs[i+1] <= revs[i]:
                return False
        return True

    @staticmethod
    def calc_purchase_total(adapter, time_info, as_of_date):
        if adapter.imports_df is None or adapter.imports_df.empty:
            return None
        df = adapter.imports_df.copy()
        start_d = time_info['start_date']
        end_d = time_info['end_date']

        if as_of_date:
            df = df[df['date'] <= as_of_date]
        if start_d:
            df = df[df['date'] >= start_d]
        if end_d:
            df = df[df['date'] <= end_d]

        # Filter out cancelled imports (Rulebook §72)
        df = df[~df['status'].apply(is_cancelled)]
        if df.empty:
            return None

        if '_is_receipt_total' in df.columns and df['_is_receipt_total'].all():
            total = df.drop_duplicates(subset=['import_id'])['total_amount'].sum()
        else:
            total = df['total_amount'].sum()

        if total == 0:
            return None
        return format_number(total, is_currency=True)

    @staticmethod
    def calc_prescription_rate(adapter, time_info, as_of_date):
        valid_items = filter_items_in_period(adapter.items_df, time_info['start_date'], time_info['end_date'], as_of_date)
        if valid_items.empty or adapter.products_df is None:
            return None

        if 'is_prescription' not in adapter.products_df.columns:
            return None

        rx_prods = adapter.products_df[adapter.products_df['is_prescription'] == True]
        rx_codes = set(rx_prods['item_code'])
        rx_names = set(rx_prods['item_name']) if 'item_name' in rx_prods else set()

        total_rev = valid_items['amount'].sum()
        if total_rev == 0:
            return None

        is_rx = valid_items['item_code'].isin(rx_codes) | valid_items['item_name'].isin(rx_names)
        rx_rev = valid_items[is_rx]['amount'].sum()
        pct = (rx_rev / total_rev) * 100.0
        return format_percent(pct)

    @staticmethod
    def calc_prescription_revenue(adapter, time_info, as_of_date):
        valid_items = filter_items_in_period(adapter.items_df, time_info['start_date'], time_info['end_date'], as_of_date)
        if valid_items.empty or adapter.products_df is None:
            return None

        if 'is_prescription' not in adapter.products_df.columns:
            return None

        rx_prods = adapter.products_df[adapter.products_df['is_prescription'] == True]
        rx_codes = set(rx_prods['item_code'])
        rx_names = set(rx_prods['item_name']) if 'item_name' in rx_prods else set()

        is_rx = valid_items['item_code'].isin(rx_codes) | valid_items['item_name'].isin(rx_names)
        rx_rev = valid_items[is_rx]['amount'].sum()
        if rx_rev == 0:
            return None
        return format_number(rx_rev, is_currency=True)

    @staticmethod
    def calc_active_ingredient_revenue(adapter, time_info, as_of_date, active_name):
        valid_items = filter_items_in_period(adapter.items_df, time_info['start_date'], time_info['end_date'], as_of_date)
        if valid_items.empty or adapter.products_df is None:
            return None

        if 'active_ingredient' not in adapter.products_df.columns:
            return None

        prod = adapter.products_df[adapter.products_df['active_ingredient'].str.lower().str.strip() == active_name.lower().strip()]
        if prod.empty:
            return None

        codes = set(prod['item_code'])
        names = set(prod['item_name']) if 'item_name' in prod else set()
        is_act = valid_items['item_code'].isin(codes) | valid_items['item_name'].isin(names)
        rev = valid_items[is_act]['amount'].sum()
        if rev == 0:
            return None
        return format_number(rev, is_currency=True)

    @staticmethod
    def calc_top_debt_customers(adapter, as_of_date, k=3):
        df = adapter.orders_df
        if df is None or df.empty:
            return None

        df = df[~df['status'].apply(is_cancelled)]
        df = df[~df['payment_method'].str.lower().str.contains('cod|sàn|shopee|lazada|tiki|tiktok')]
        if as_of_date:
            df = df[df['date'] <= as_of_date]

        df = df[df['debt'] > 0]
        df = df[~df['cust_name'].str.lower().str.contains('khách lẻ|khach le|^$')]
        if df.empty:
            return None

        grouped = df.groupby(['cust_id', 'cust_name'])['debt'].sum().reset_index()
        grouped = grouped.sort_values(by=['debt', 'cust_id'], ascending=[False, True])

        rows = []
        for _, r in grouped.head(k).iterrows():
            rows.append([r['cust_name'], int(round(r['debt']))])

        if not rows:
            return None
        return format_table(["khach_hang", "so_tien_no"], rows)

    @staticmethod
    def calc_unsold_items(adapter, time_info, as_of_date):
        # Rulebook §77: Hàng ế = đã từng bán trước kỳ nhưng trong kỳ không có đơn nào
        if adapter.items_df is None or adapter.items_df.empty:
            return None

        start_d = time_info['start_date']
        end_d = time_info['end_date']

        before_items = filter_items_in_period(adapter.items_df, '2000-01-01', start_d, as_of_date)
        before_items = before_items[before_items['date'] < start_d]
        before_items = before_items[~before_items['item_name'].str.lower().str.contains('phí phục vụ')]
        before_codes = set(before_items['item_code'])

        curr_items = filter_items_in_period(adapter.items_df, start_d, end_d, as_of_date)
        curr_codes = set(curr_items['item_code'])

        unsold_codes = before_codes - curr_codes
        if not unsold_codes:
            return []

        if adapter.products_df is not None and not adapter.products_df.empty:
            sub_p = adapter.products_df[adapter.products_df['item_code'].isin(unsold_codes)].sort_values('item_code')
            if not sub_p.empty:
                names = sub_p['item_name'].tolist()
            else:
                names = before_items[before_items['item_code'].isin(unsold_codes)].drop_duplicates('item_code').sort_values('item_code')['item_name'].tolist()
        else:
            names = before_items[before_items['item_code'].isin(unsold_codes)].drop_duplicates('item_code').sort_values('item_code')['item_name'].tolist()

        clean = []
        for n in names:
            n_str = str(n).strip()
            nl = n_str.lower()
            if nl in ['khách lẻ', 'khach le', 'nan', 'none', '', 'null', 'tong cong', 'tổng cộng', 'đơn hàng']:
                continue
            if nl.startswith(('ord', 'hd_', 'hđ_')):
                continue
            clean.append(n_str)
        return clean

    @staticmethod
    def calc_item_qty_sold(adapter, time_info, as_of_date, item_query, unit_name=None):
        valid_items = filter_items_in_period(adapter.items_df, time_info['start_date'], time_info['end_date'], as_of_date)
        if valid_items.empty:
            return None
        matches = match_item_rows(valid_items, item_query)
        if matches.empty:
            return None
        
        total_qty = matches['qty_base'].sum()
        if total_qty == 0:
            return None
        return format_number(total_qty, is_currency=False)

    @staticmethod
    def calc_avg_import_price(adapter, time_info, as_of_date, item_query):
        if adapter.imports_df is None or adapter.imports_df.empty:
            return None
        df = adapter.imports_df.copy()
        start_d = time_info['start_date']
        end_d = time_info['end_date']
        if as_of_date:
            df = df[df['date'] <= as_of_date]
        if start_d:
            df = df[df['date'] >= start_d]
        if end_d:
            df = df[df['date'] <= end_d]
        df = df[~df['status'].apply(is_cancelled)]

        matches = match_item_rows(df, item_query)
        if matches.empty:
            return None
        
        tot_amt = matches['total_amount'].sum()
        tot_qty = matches['qty'].sum()
        if tot_qty == 0 or tot_amt == 0:
            return None
        avg_price = tot_amt / tot_qty
        return format_number(avg_price, is_currency=True)

    @staticmethod
    def calc_item_revenue(adapter, time_info, as_of_date, item_query):
        valid_items = filter_items_in_period(adapter.items_df, time_info['start_date'], time_info['end_date'], as_of_date)
        if valid_items.empty:
            return None
        matches = match_item_rows(valid_items, item_query)
        if matches.empty:
            return None
        total_rev = matches['amount'].sum()
        if total_rev == 0:
            return None
        return format_number(total_rev, is_currency=True)

    @staticmethod
    def calc_monthly_revenue_list(adapter, periods, as_of_date):
        if not periods:
            return None
        revs = []
        for p in periods:
            r = QueryExecutors.calc_revenue(adapter, p, as_of_date)
            if r is None:
                return None
            revs.append(int(round(r)))
        return revs

    @staticmethod
    def calc_highest_revenue_month(adapter, periods, as_of_date):
        if not periods:
            return None
        best_label = None
        best_rev = -1
        for p in periods:
            r = QueryExecutors.calc_revenue(adapter, p, as_of_date)
            if r is not None and r > best_rev:
                best_rev = r
                best_label = p.get('month_label')
        return best_label if best_rev > 0 else None

    @staticmethod
    def calc_days_above_revenue(adapter, time_info, as_of_date, min_revenue):
        valid_items = filter_items_in_period(adapter.items_df, time_info['start_date'], time_info['end_date'], as_of_date)
        if valid_items.empty or min_revenue <= 0:
            return None
        daily = valid_items.groupby('date')['amount'].sum()
        cnt = int((daily >= min_revenue).sum())
        return cnt if cnt > 0 else None

    @staticmethod
    def calc_item_unit_price(adapter, time_info, as_of_date, item_query):
        valid_items = filter_items_in_period(adapter.items_df, time_info['start_date'], time_info['end_date'], as_of_date)
        if valid_items.empty:
            return None
        matches = match_item_rows(valid_items, item_query)
        if matches.empty:
            return None
        # Filter base unit rows if unit_price > 0
        pos = matches[matches['unit_price'] > 0]
        if pos.empty:
            return None
        # If base_unit is known or min unit_price (base unit has smallest unit_price when larger units exist)
        u_price = pos['unit_price'].min()
        return format_number(u_price, is_currency=True)

    @staticmethod
    def calc_top_customers(adapter, time_info, as_of_date, k=1):
        valid_items = filter_items_in_period(adapter.items_df, time_info['start_date'], time_info['end_date'], as_of_date)
        if valid_items.empty or 'cust_name' not in valid_items.columns:
            return None
        df = valid_items.copy()
        df['cust_name_clean'] = df['cust_name'].fillna('').astype(str).str.strip()
        df = df[~df['cust_name_clean'].str.lower().isin(['', 'nan', 'none', 'khách lẻ', 'khach le', 'khách vãng lai'])]
        if df.empty:
            return None
        df['cust_id_clean'] = df['cust_id'].fillna('').astype(str).str.strip() if 'cust_id' in df.columns else df['cust_name_clean']
        grouped = df.groupby(['cust_id_clean', 'cust_name_clean'])['amount'].sum().reset_index()
        grouped = grouped.sort_values(by=['amount', 'cust_id_clean'], ascending=[False, True])
        top_names = grouped['cust_name_clean'].head(k).tolist()
        if not top_names:
            return None
        return top_names if k > 1 else top_names[0]


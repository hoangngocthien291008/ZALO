import re
from datetime import datetime, timedelta
import calendar

class TimeParser:
    def __init__(self, calendar_resolver=None):
        self.cal = calendar_resolver

    def parse_time_period(self, text, as_of_date_str):
        """
        Parses time expressions from Vietnamese query text relative to as_of_date_str.
        Returns a dict:
        {
            'start_date': 'YYYY-MM-DD',
            'end_date': 'YYYY-MM-DD',
            'compare_start_date': 'YYYY-MM-DD' or None,
            'compare_end_date': 'YYYY-MM-DD' or None,
            'shift': 'morning' | 'afternoon' | None,
            'raw_type': str
        }
        """
        text_lower = text.lower().strip()
        text_lower = re.sub(r'\b(hom ni|hôm ni|hnay|hom nay)\b', 'hôm nay', text_lower)
        text_lower = re.sub(r'\b(hqua|bữa qua|bua qua|hom qua)\b', 'hôm qua', text_lower)
        text_lower = re.sub(r'\b(thangs|thang)\b', 'tháng', text_lower)
        text_lower = re.sub(r'\btháng nay\b', 'tháng này', text_lower)
        text_lower = re.sub(r'\b(tu dau|từ dau)\b', 'từ đầu', text_lower)
        text_lower = re.sub(r'\b(den het|đến het|den hết)\b', 'đến hết', text_lower)
        text_lower = re.sub(r'\b(heets|het)\b', 'hết', text_lower)
        text_lower = re.sub(r'\bth(\d{1,2})[/-](\d{2,4})\b', r'tháng \1/\2', text_lower)
        text_lower = re.sub(r'\bt(\d{1,2})[/-](\d{2,4})\b', r'tháng \1/\2', text_lower)
        as_of = datetime.strptime(as_of_date_str, '%Y-%m-%d').date()

        # Check shift
        shift = None
        if 'ca sáng' in text_lower or 'ca sang' in text_lower:
            shift = 'morning'
        elif 'ca chiều' in text_lower or 'ca chieu' in text_lower:
            shift = 'afternoon'

        # 1. Tet period
        # "tết năm 2026", "tết 2026", "dịp tết 2026", "mùa tết"
        m_tet = re.search(r'tết\s*(?:năm\s*)?(\d{4})?', text_lower)
        if m_tet and self.cal:
            year = int(m_tet.group(1)) if m_tet.group(1) else as_of.year
            start_date, end_date, b_start, b_end = self.cal.get_tet_range(year)
            if 'so với trước tết' in text_lower or 'so với trc tết' in text_lower or 'so voi truoc tet' in text_lower:
                return {
                    'start_date': start_date,
                    'end_date': end_date,
                    'compare_start_date': b_start,
                    'compare_end_date': b_end,
                    'shift': shift,
                    'raw_type': 'tet_compare'
                }
            return {
                'start_date': start_date,
                'end_date': end_date,
                'compare_start_date': None,
                'compare_end_date': None,
                'shift': shift,
                'raw_type': 'tet'
            }

        # 2. Explicit Date Range: "từ DD/MM/YYYY đến (hết) DD/MM/YYYY" (Rulebook §28)
        m_range = re.search(
            r'từ\s*(?:ngày\s*)?(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\s*(?:đến|tới|den|toi|-)\s*(?:hết\s*)?(?:ngày\s*)?(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})',
            text_lower
        )
        if m_range:
            d1, m1, y1 = int(m_range.group(1)), int(m_range.group(2)), m_range.group(3)
            d2, m2, y2 = int(m_range.group(4)), int(m_range.group(5)), int(m_range.group(6))
            if y2 < 100:
                y2 += 2000
            y1 = int(y1) if y1 else y2
            if y1 < 100:
                y1 += 2000
            start_d = f"{y1:04d}-{m1:02d}-{d1:02d}"
            end_d = f"{y2:04d}-{m2:02d}-{d2:02d}"
            return {
                'start_date': start_d,
                'end_date': end_d,
                'compare_start_date': None,
                'compare_end_date': None,
                'shift': shift,
                'raw_type': 'date_range'
            }

        # 3. "từ đầu năm", "từ 1/1 năm nay", "từ 01/01 năm nay" (Rulebook §34: 01/01 đến hết hôm nay)
        if any(k in text_lower for k in ['đầu năm', 'dau nam', '1/1 năm nay', '01/01 năm nay', '1/1 nam nay', '01/01 nam nay']):
            start_d = f"{as_of.year:04d}-01-01"
            return {
                'start_date': start_d,
                'end_date': as_of.strftime('%Y-%m-%d'),
                'compare_start_date': None,
                'compare_end_date': None,
                'shift': shift,
                'raw_type': 'ytd'
            }

        # 4. "hôm nay", "trong ngày hôm nay", "ngày hôm nay"
        if 'hôm nay' in text_lower and 'từ đầu' not in text_lower and 'tính đến' not in text_lower and 'đến hết' not in text_lower and 'tháng này' not in text_lower and 'tuần này' not in text_lower:
            d_str = as_of.strftime('%Y-%m-%d')
            return {
                'start_date': d_str,
                'end_date': d_str,
                'compare_start_date': (as_of - timedelta(days=1)).strftime('%Y-%m-%d'),
                'compare_end_date': (as_of - timedelta(days=1)).strftime('%Y-%m-%d'),
                'shift': shift,
                'raw_type': 'today'
            }

        # 5. "hôm qua"
        if 'hôm qua' in text_lower:
            d_str = (as_of - timedelta(days=1)).strftime('%Y-%m-%d')
            return {
                'start_date': d_str,
                'end_date': d_str,
                'compare_start_date': (as_of - timedelta(days=2)).strftime('%Y-%m-%d'),
                'compare_end_date': (as_of - timedelta(days=2)).strftime('%Y-%m-%d'),
                'shift': shift,
                'raw_type': 'yesterday'
            }

        # 6. "N ngày gần nhất" (Rulebook §29: N ngày liền nhau kể cả hôm nay)
        m_ndays = re.search(r'(\d+)\s*ngày\s*gần\s*(?:nhất|đây)', text_lower)
        if not m_ndays:
            m_ndays = re.search(r'(\d+)\s*ngay\s*gan\s*(?:nhat|day)', text_lower)
        if m_ndays:
            n = int(m_ndays.group(1))
            start_d = as_of - timedelta(days=n - 1)
            return {
                'start_date': start_d.strftime('%Y-%m-%d'),
                'end_date': as_of.strftime('%Y-%m-%d'),
                'compare_start_date': None,
                'compare_end_date': None,
                'shift': shift,
                'raw_type': 'n_days'
            }

        # 7. Explicit date: "ngày DD/MM/YYYY" or "DD/MM/YYYY"
        m_date = re.search(r'(?:ngày\s*)?(\d{1,2})[/-](\d{1,2})[/-](\d{4})', text_lower)
        if m_date:
            day = int(m_date.group(1))
            month = int(m_date.group(2))
            year = int(m_date.group(3))
            d_str = f"{year:04d}-{month:02d}-{day:02d}"
            return {
                'start_date': d_str,
                'end_date': d_str,
                'compare_start_date': None,
                'compare_end_date': None,
                'shift': shift,
                'raw_type': 'exact_date'
            }

        # 7b. Explicit date without year: "ngày DD/MM" or "hôm DD/MM"
        m_date_short = re.search(r'(?:ngày|ngay|hôm|hom)\s+(\d{1,2})[/-](\d{1,2})(?![/-]\d)', text_lower)
        if m_date_short:
            day = int(m_date_short.group(1))
            month = int(m_date_short.group(2))
            if 1 <= day <= 31 and 1 <= month <= 12:
                year = as_of.year if (month, day) <= (as_of.month, as_of.day) else as_of.year - 1
                d_str = f"{year:04d}-{month:02d}-{day:02d}"
                return {
                    'start_date': d_str,
                    'end_date': d_str,
                    'compare_start_date': None,
                    'compare_end_date': None,
                    'shift': shift,
                    'raw_type': 'exact_date_short'
                }

        # 8. "tuần này" (Rulebook §31: từ thứ Hai của tuần chứa hôm nay đến hết hôm nay)
        if 'tuần này' in text_lower or 'tuan nay' in text_lower:
            start_d = as_of - timedelta(days=as_of.weekday())
            return {
                'start_date': start_d.strftime('%Y-%m-%d'),
                'end_date': as_of.strftime('%Y-%m-%d'),
                'compare_start_date': None,
                'compare_end_date': None,
                'shift': shift,
                'raw_type': 'this_week'
            }

        # 9. "tuần trước", "tuần rồi" (cả tuần liền trước, thứ Hai đến Chủ nhật)
        if 'tuần trước' in text_lower or 'tuần rồi' in text_lower or 'tuan truoc' in text_lower or 'tuan roi' in text_lower:
            monday_this_week = as_of - timedelta(days=as_of.weekday())
            sunday_last_week = monday_this_week - timedelta(days=1)
            monday_last_week = sunday_last_week - timedelta(days=6)
            return {
                'start_date': monday_last_week.strftime('%Y-%m-%d'),
                'end_date': sunday_last_week.strftime('%Y-%m-%d'),
                'compare_start_date': None,
                'compare_end_date': None,
                'shift': shift,
                'raw_type': 'last_week'
            }

        # 10. "từ đầu năm", "từ đầu năm tới nay", "từ đầu năm đến nay" (Rulebook §34: 01/01 đến hết hôm nay)
        if 'đầu năm' in text_lower or 'dau nam' in text_lower:
            start_d = f"{as_of.year:04d}-01-01"
            return {
                'start_date': start_d,
                'end_date': as_of.strftime('%Y-%m-%d'),
                'compare_start_date': None,
                'compare_end_date': None,
                'shift': shift,
                'raw_type': 'ytd'
            }

        # 11. Quarter MUST be checked BEFORE Month so "quý 2/2026" is not parsed as Month 2/2026!
        m_q = re.search(r'(?:quý|quy)\s*(\d)(?:[\s/-]+(?:năm\s*|nam\s*)?(\d{4}))?', text_lower)
        if m_q:
            q = int(m_q.group(1))
            if 1 <= q <= 4:
                start_month = (q - 1) * 3 + 1
                end_month = q * 3
                if m_q.group(2):
                    year = int(m_q.group(2))
                else:
                    curr_q = (as_of.month - 1) // 3 + 1
                    year = as_of.year - 1 if q > curr_q else as_of.year
                _, last_day = calendar.monthrange(year, end_month)
                start_d = f"{year:04d}-{start_month:02d}-01"
                end_d = f"{year:04d}-{end_month:02d}-{last_day:02d}"

                # Previous quarter for comparison
                prev_q = q - 1 if q > 1 else 4
                prev_y = year if q > 1 else year - 1
                p_start_m = (prev_q - 1) * 3 + 1
                p_end_m = prev_q * 3
                _, p_last_d = calendar.monthrange(prev_y, p_end_m)
                cmp_start = f"{prev_y:04d}-{p_start_m:02d}-01"
                cmp_end = f"{prev_y:04d}-{p_end_m:02d}-{p_last_d:02d}"

                return {
                    'start_date': start_d,
                    'end_date': end_d,
                    'compare_start_date': cmp_start,
                    'compare_end_date': cmp_end,
                    'shift': shift,
                    'raw_type': 'quarter'
                }

        # 12. "tháng này" / "đầu tháng này" (Rulebook §33: từ ngày 1 đến hết hôm nay)
        if 'tháng này' in text_lower or 'thang nay' in text_lower:
            start_d = f"{as_of.year:04d}-{as_of.month:02d}-01"
            end_d = as_of.strftime('%Y-%m-%d')
            prev_year = as_of.year if as_of.month > 1 else as_of.year - 1
            prev_month = as_of.month - 1 if as_of.month > 1 else 12
            _, prev_month_days = calendar.monthrange(prev_year, prev_month)
            compare_day = min(as_of.day, prev_month_days)
            cmp_start = f"{prev_year:04d}-{prev_month:02d}-01"
            cmp_end = f"{prev_year:04d}-{prev_month:02d}-{compare_day:02d}"

            return {
                'start_date': start_d,
                'end_date': end_d,
                'compare_start_date': cmp_start,
                'compare_end_date': cmp_end,
                'shift': shift,
                'raw_type': 'this_month'
            }

        # 12b. "tháng trước" / "tháng rồi" (when NOT preceded by "so với" after a specific month)
        if ('tháng trước' in text_lower or 'tháng rồi' in text_lower) and not re.search(r'tháng\s*\d', text_lower) and 'so với tháng trước' not in text_lower:
            prev_year = as_of.year if as_of.month > 1 else as_of.year - 1
            prev_month = as_of.month - 1 if as_of.month > 1 else 12
            _, last_day = calendar.monthrange(prev_year, prev_month)
            start_d = f"{prev_year:04d}-{prev_month:02d}-01"
            end_d = f"{prev_year:04d}-{prev_month:02d}-{last_day:02d}"

            pp_year = prev_year if prev_month > 1 else prev_year - 1
            pp_month = prev_month - 1 if prev_month > 1 else 12
            _, pp_last = calendar.monthrange(pp_year, pp_month)
            return {
                'start_date': start_d,
                'end_date': end_d,
                'compare_start_date': f"{pp_year:04d}-{pp_month:02d}-01",
                'compare_end_date': f"{pp_year:04d}-{pp_month:02d}-{pp_last:02d}",
                'shift': shift,
                'raw_type': 'last_month'
            }

        # 13. Month M/Y or "tháng M năm Y" or "tháng M"
        all_months = list(re.finditer(r'tháng\s*(\d{1,2})(?:[\s/-]+(?:năm\s*|nam\s*)?(\d{2,4}))?', text_lower))
        if not all_months:
            all_months = list(re.finditer(r'(?<!\d[/-])\b(\d{1,2})[/-](202\d)\b', text_lower))

        if all_months:
            def resolve_m(m_obj):
                m_val = int(m_obj.group(1))
                if not (1 <= m_val <= 12):
                    return None
                if m_obj.group(2):
                    y_val = int(m_obj.group(2))
                    if y_val < 100:
                        y_val += 2000
                else:
                    y_val = as_of.year - 1 if m_val > as_of.month else as_of.year
                _, l_day = calendar.monthrange(y_val, m_val)
                return m_val, y_val, f"{y_val:04d}-{m_val:02d}-01", f"{y_val:04d}-{m_val:02d}-{l_day:02d}"

            res1 = resolve_m(all_months[0])
            if res1:
                month, year, start_d, end_d = res1
                # Determine comparison period
                if len(all_months) >= 2 and ('so với' in text_lower or 'so voi' in text_lower or 'với tháng' in text_lower):
                    res2 = resolve_m(all_months[1])
                    if res2:
                        _, _, cmp_start, cmp_end = res2
                    else:
                        cmp_start, cmp_end = None, None
                elif any(k in text_lower for k in ['cùng kỳ năm trước', 'cung ky nam truoc', 'so với năm trước', 'so voi nam truoc', 'năm ngoái', 'nam ngoai']):
                    cmp_y = year - 1
                    _, p_last_day = calendar.monthrange(cmp_y, month)
                    cmp_start = f"{cmp_y:04d}-{month:02d}-01"
                    cmp_end = f"{cmp_y:04d}-{month:02d}-{p_last_day:02d}"
                else:
                    prev_year = year if month > 1 else year - 1
                    prev_month = month - 1 if month > 1 else 12
                    _, p_last_day = calendar.monthrange(prev_year, prev_month)
                    cmp_start = f"{prev_year:04d}-{prev_month:02d}-01"
                    cmp_end = f"{prev_year:04d}-{prev_month:02d}-{p_last_day:02d}"

                return {
                    'start_date': start_d,
                    'end_date': end_d,
                    'compare_start_date': cmp_start,
                    'compare_end_date': cmp_end,
                    'shift': shift,
                    'raw_type': 'specific_month'
                }

        # Default fallback: today
        d_str = as_of.strftime('%Y-%m-%d')
        return {
            'start_date': d_str,
            'end_date': d_str,
            'compare_start_date': None,
            'compare_end_date': None,
            'shift': shift,
            'raw_type': 'default_today'
        }

import csv
from datetime import datetime, timedelta
import calendar

class CalendarResolver:
    def __init__(self, calendar_path=None):
        self.date_to_lunar = {}
        self.lunar_to_date = {}
        self.date_to_info = {}
        if calendar_path:
            self.load_calendar(calendar_path)

    def load_calendar(self, calendar_path):
        with open(calendar_path, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                d_str = row['date'].strip()
                l_day = int(row['lunar_day'])
                l_month = int(row['lunar_month'])
                l_year = int(row['lunar_year'])
                w_day = int(row['weekday'])
                holiday = row.get('holiday', '').strip()

                self.date_to_lunar[d_str] = (l_day, l_month, l_year)
                self.lunar_to_date[(l_year, l_month, l_day)] = d_str
                self.date_to_info[d_str] = {
                    'weekday': w_day,
                    'lunar': (l_day, l_month, l_year),
                    'holiday': holiday
                }

    def get_tet_range(self, tet_year):
        """
        Rulebook §35:
        Tết năm Y, dịp Tết, mùa Tết: từ ngày dương của 23 tháng Chạp (năm âm Y - 1)
        đến hết ngày dương của mùng 10 tháng Giêng (năm âm Y).
        So với trước Tết = cùng số ngày ngay trước 23 tháng Chạp.
        """
        # 23 tháng Chạp năm âm Y - 1
        start_date = self.lunar_to_date.get((tet_year - 1, 12, 23))
        # mùng 10 tháng Giêng năm âm Y
        end_date = self.lunar_to_date.get((tet_year, 1, 10))

        if not start_date or not end_date:
            return None, None, None, None

        d_start = datetime.strptime(start_date, '%Y-%m-%d').date()
        d_end = datetime.strptime(end_date, '%Y-%m-%d').date()
        num_days = (d_end - d_start).days + 1

        d_before_end = d_start - timedelta(days=1)
        d_before_start = d_before_end - timedelta(days=num_days - 1)

        before_start_date = d_before_start.strftime('%Y-%m-%d')
        before_end_date = d_before_end.strftime('%Y-%m-%d')

        return start_date, end_date, before_start_date, before_end_date

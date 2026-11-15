import ast
import contextlib
import io
import math
import multiprocessing as mp
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .entity_matcher import EntityResult, match_item_rows, normalize_query_str
from .formatters import (
    format_boolean,
    format_date,
    format_list,
    format_number,
    format_percent,
    format_string,
    format_table,
    round_half_up,
    to_native,
)
from .preprocessor import strip_accents
from .schema_builder import (
    BusinessAdapter,
    ShopBundle,
    clean_currency_str,
    is_cancelled_status,
    is_returned_status,
)
from .time_resolver import CalendarResolver, TimeContext

UNANSWERABLE_SENTINEL = "__DATAMATE_UNANSWERABLE__"


def is_warranty_status(status_str: Any) -> bool:
    s = str(status_str).lower().strip()
    return "bảo hành" in s or "bao hanh" in s


def filter_items_in_period(
    items_df: Optional[pd.DataFrame],
    start_date: Optional[str],
    end_date: Optional[str],
    as_of_date: Optional[str] = None,
    shift: Optional[str] = None,
) -> pd.DataFrame:
    """
    Kế thừa từ query_executors.py cũ:
    - Lọc bản ghi trong khoảng [start_date, end_date], loại bỏ bản ghi sau as_of_date (Rulebook §5).
    - Lọc theo ca sáng (06:00-13:59) / ca chiều (14:00-21:59) (Rulebook §2 Mục 37).
    - Loại bỏ hoàn toàn đơn huỷ và đơn trả hàng (kể cả trả 1 phần -> loại cả đơn, Rulebook §1).
    """
    if items_df is None or items_df.empty:
        return pd.DataFrame()

    df = items_df.copy()

    if as_of_date and "date" in df.columns:
        df = df[df["date"] <= as_of_date]

    if start_date and "date" in df.columns:
        df = df[df["date"] >= start_date]
    if end_date and "date" in df.columns:
        df = df[df["date"] <= end_date]

    if shift and "raw_date" in df.columns:

        def match_shift(val: Any) -> bool:
            s = str(val)
            m = re.search(r"(\d{1,2}):(\d{2})", s)
            if not m:
                return True
            hour = int(m.group(1))
            if shift == "morning":
                return 6 <= hour <= 13
            elif shift == "afternoon":
                return 14 <= hour <= 21
            return True

        df = df[df["raw_date"].apply(match_shift)]

    if "is_valid" in df.columns:
        df = df[df["is_valid"] == True]
    elif "status" in df.columns:
        if "order_id" in df.columns:
            bad_orders = set(
                df[
                    df["status"].apply(
                        lambda s: is_cancelled_status(s) or is_returned_status(s)
                    )
                ]["order_id"]
                .dropna()
                .unique()
            )
            df = df[~df["order_id"].isin(bad_orders)]
        else:
            df = df[
                ~df["status"].apply(
                    lambda s: is_cancelled_status(s) or is_returned_status(s)
                )
            ]

    return df


class QueryExecutors:
    """
    Kế thừa 100% các hàm tính toán tất định từ bài làm nộp thử/datamate/query_executors.py,
    dùng làm thư viện Helper chuẩn trong Sandbox và bộ Kiểm chứng chéo (Cross-check Gate).
    """

    @staticmethod
    def calc_revenue(
        adapter: BusinessAdapter, time_info: Dict[str, Any], as_of_date: str
    ) -> Optional[int]:
        start_d = time_info["start_date"]
        end_d = time_info["end_date"]
        shift = time_info.get("shift")

        valid_items = filter_items_in_period(
            adapter.items_df, start_d, end_d, as_of_date, shift
        )
        if valid_items.empty:
            return None
        total_rev = valid_items["amount"].sum()
        if total_rev == 0:
            return None
        return format_number(total_rev, is_currency=True)

    @staticmethod
    def calc_aov(
        adapter: BusinessAdapter, time_info: Dict[str, Any], as_of_date: str
    ) -> Optional[int]:
        start_d = time_info["start_date"]
        end_d = time_info["end_date"]
        shift = time_info.get("shift")

        valid_items = filter_items_in_period(
            adapter.items_df, start_d, end_d, as_of_date, shift
        )
        if valid_items.empty:
            return None
        if "status" in valid_items.columns:
            valid_items = valid_items[
                ~(
                    valid_items["status"].apply(is_warranty_status)
                    & (valid_items["amount"] == 0)
                )
            ]
        if valid_items.empty:
            return None
        total_rev = valid_items["amount"].sum()
        order_count = len(valid_items["order_id"].unique())
        if order_count == 0 or total_rev == 0:
            return None
        return int((total_rev / order_count) + 0.5)

    @staticmethod
    def calc_revenue_diff(
        adapter: BusinessAdapter,
        time_info: Dict[str, Any],
        as_of_date: str,
        is_percent: bool = False,
    ) -> Optional[Any]:
        start_d = time_info["start_date"]
        end_d = time_info["end_date"]
        cmp_start = time_info.get("compare_start_date")
        cmp_end = time_info.get("compare_end_date")
        shift = time_info.get("shift")
        cmp_shift = time_info.get("cmp_shift")

        if cmp_shift and not cmp_start:
            cmp_start, cmp_end = start_d, end_d

        if not cmp_start or not cmp_end:
            return None

        curr_items = filter_items_in_period(
            adapter.items_df, start_d, end_d, as_of_date, shift
        )
        prev_items = filter_items_in_period(
            adapter.items_df, cmp_start, cmp_end, as_of_date, cmp_shift or shift
        )

        rev_curr = curr_items["amount"].sum() if not curr_items.empty else 0.0
        rev_prev = prev_items["amount"].sum() if not prev_items.empty else 0.0

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
    def calc_cancellation_rate(
        adapter: BusinessAdapter, time_info: Dict[str, Any], as_of_date: str
    ) -> Optional[float]:
        start_d = time_info["start_date"]
        end_d = time_info["end_date"]

        df = adapter.orders_df
        if df is None or df.empty:
            if adapter.items_df is None or adapter.items_df.empty:
                return None
            df = adapter.items_df[["order_id", "date", "status"]].drop_duplicates()

        sub = df[(df["date"] >= start_d) & (df["date"] <= end_d)]
        if as_of_date:
            sub = sub[sub["date"] <= as_of_date]

        if sub.empty:
            return None

        total_orders = len(sub["order_id"].unique())
        cancelled_orders = len(
            sub[sub["status"].apply(is_cancelled_status)]["order_id"].unique()
        )

        if total_orders == 0:
            return None
        rate = (cancelled_orders / total_orders) * 100.0
        return format_percent(rate)

    @staticmethod
    def calc_cancelled_order_count(
        adapter: BusinessAdapter, time_info: Dict[str, Any], as_of_date: str
    ) -> int:
        start_d = time_info["start_date"]
        end_d = time_info["end_date"]

        df = adapter.orders_df
        if df is None or df.empty:
            if adapter.items_df is None or adapter.items_df.empty:
                return 0
            df = adapter.items_df[["order_id", "date", "status"]].drop_duplicates()

        sub = df[(df["date"] >= start_d) & (df["date"] <= end_d)]
        if as_of_date:
            sub = sub[sub["date"] <= as_of_date]

        if sub.empty:
            return 0

        cancelled_orders = len(
            sub[sub["status"].apply(is_cancelled_status)]["order_id"].unique()
        )
        return int(cancelled_orders)

    @staticmethod
    def calc_order_count(
        adapter: BusinessAdapter,
        time_info: Dict[str, Any],
        as_of_date: str,
        min_revenue: float = 0,
    ) -> int:
        start_d = time_info["start_date"]
        end_d = time_info["end_date"]
        shift = time_info.get("shift")

        valid_items = filter_items_in_period(
            adapter.items_df, start_d, end_d, as_of_date, shift
        )
        if valid_items.empty:
            return 0

        order_rev = valid_items.groupby("order_id")["amount"].sum()
        if min_revenue > 0:
            order_rev = order_rev[order_rev >= min_revenue]

        return int(len(order_rev))

    @staticmethod
    def calc_best_sellers(
        adapter: BusinessAdapter,
        time_info: Dict[str, Any],
        as_of_date: str,
        k: int = 1,
        item_group: Optional[str] = None,
    ) -> Optional[Any]:
        start_d = time_info["start_date"]
        end_d = time_info["end_date"]

        valid_items = filter_items_in_period(
            adapter.items_df, start_d, end_d, as_of_date
        )
        if valid_items.empty:
            return None

        valid_items = valid_items[
            ~valid_items["item_name"].str.lower().str.contains("phí phục vụ", na=False)
        ]

        if item_group:
            grp_l = item_group.lower().strip()
            matched = False
            if "item_group" in valid_items.columns:
                sub = valid_items[
                    valid_items["item_group"]
                    .str.lower()
                    .str.contains(re.escape(grp_l), na=False)
                ]
                if not sub.empty:
                    valid_items = sub
                    matched = True
            if not matched:
                sub_name = valid_items[
                    valid_items["item_name"]
                    .str.lower()
                    .str.contains(re.escape(grp_l), na=False)
                ]
                if not sub_name.empty:
                    valid_items = sub_name

        grouped = (
            valid_items.groupby(["item_code", "item_name"])["qty_base"]
            .sum()
            .reset_index()
        )
        grouped = grouped.sort_values(
            by=["qty_base", "item_code"], ascending=[False, True], kind="mergesort"
        )

        if grouped.empty:
            return None

        top_names = grouped["item_name"].tolist()
        clean_names = []
        for n in top_names:
            n_str = str(n).strip()
            nl = n_str.lower()
            if nl in [
                "khách lẻ",
                "khach le",
                "nan",
                "none",
                "",
                "null",
                "tong cong",
                "tổng cộng",
                "đơn hàng",
            ]:
                continue
            if nl.startswith(("ord", "hd_", "hđ_")):
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
    def calc_highest_revenue_date(
        adapter: BusinessAdapter, time_info: Dict[str, Any], as_of_date: str
    ) -> Optional[str]:
        start_d = time_info["start_date"]
        end_d = time_info["end_date"]

        valid_items = filter_items_in_period(
            adapter.items_df, start_d, end_d, as_of_date
        )
        if valid_items.empty:
            return None

        daily = (
            valid_items[valid_items["date"] != ""]
            .groupby("date")["amount"]
            .sum()
            .reset_index()
        )
        daily = daily.sort_values(
            by=["amount", "date"], ascending=[False, True], kind="mergesort"
        )
        if daily.empty:
            return None
        return format_date(daily.iloc[0]["date"])

    @staticmethod
    def calc_continuous_increase(
        adapter: BusinessAdapter, periods: List[Dict[str, Any]], as_of_date: str
    ) -> Optional[bool]:
        if not periods or len(periods) < 2:
            return None
        revs = []
        for p in periods:
            items = filter_items_in_period(
                adapter.items_df, p["start_date"], p["end_date"], as_of_date
            )
            rev = items["amount"].sum() if not items.empty else 0.0
            revs.append(rev)

        if any(r == 0 for r in revs):
            return None

        for i in range(len(revs) - 1):
            if revs[i + 1] <= revs[i]:
                return False
        return True

    @staticmethod
    def calc_purchase_total(
        adapter: BusinessAdapter, time_info: Dict[str, Any], as_of_date: str
    ) -> Optional[int]:
        if adapter.imports_df is None or adapter.imports_df.empty:
            return None
        df = adapter.imports_df.copy()
        start_d = time_info["start_date"]
        end_d = time_info["end_date"]

        if as_of_date:
            df = df[df["date"] <= as_of_date]
        if start_d:
            df = df[df["date"] >= start_d]
        if end_d:
            df = df[df["date"] <= end_d]

        df = df[~df["status"].apply(is_cancelled_status)]
        if df.empty:
            return None

        if "_is_receipt_total" in df.columns and df["_is_receipt_total"].all():
            total = df.drop_duplicates(subset=["import_id"])["total_amount"].sum()
        else:
            total = df["total_amount"].sum()

        if total == 0:
            return None
        return format_number(total, is_currency=True)

    @staticmethod
    def calc_prescription_rate(
        adapter: BusinessAdapter, time_info: Dict[str, Any], as_of_date: str
    ) -> Optional[float]:
        valid_items = filter_items_in_period(
            adapter.items_df,
            time_info["start_date"],
            time_info["end_date"],
            as_of_date,
        )
        if valid_items.empty or adapter.products_df is None:
            return None
        if "is_prescription" not in adapter.products_df.columns:
            return None

        rx_prods = adapter.products_df[adapter.products_df["is_prescription"] == True]
        rx_codes = set(rx_prods["item_code"])
        rx_names = (
            set(rx_prods["item_name"]) if "item_name" in rx_prods else set()
        )

        total_rev = valid_items["amount"].sum()
        if total_rev == 0:
            return None

        is_rx = valid_items["item_code"].isin(rx_codes) | valid_items[
            "item_name"
        ].isin(rx_names)
        rx_rev = valid_items[is_rx]["amount"].sum()
        pct = (rx_rev / total_rev) * 100.0
        return format_percent(pct)

    @staticmethod
    def calc_prescription_revenue(
        adapter: BusinessAdapter, time_info: Dict[str, Any], as_of_date: str
    ) -> Optional[int]:
        valid_items = filter_items_in_period(
            adapter.items_df,
            time_info["start_date"],
            time_info["end_date"],
            as_of_date,
        )
        if valid_items.empty or adapter.products_df is None:
            return None
        if "is_prescription" not in adapter.products_df.columns:
            return None

        rx_prods = adapter.products_df[adapter.products_df["is_prescription"] == True]
        rx_codes = set(rx_prods["item_code"])
        rx_names = (
            set(rx_prods["item_name"]) if "item_name" in rx_prods else set()
        )

        is_rx = valid_items["item_code"].isin(rx_codes) | valid_items[
            "item_name"
        ].isin(rx_names)
        rx_rev = valid_items[is_rx]["amount"].sum()
        if rx_rev == 0:
            return None
        return format_number(rx_rev, is_currency=True)

    @staticmethod
    def calc_active_ingredient_revenue(
        adapter: BusinessAdapter,
        time_info: Dict[str, Any],
        as_of_date: str,
        active_name: str,
    ) -> Optional[int]:
        valid_items = filter_items_in_period(
            adapter.items_df,
            time_info["start_date"],
            time_info["end_date"],
            as_of_date,
        )
        if valid_items.empty or adapter.products_df is None:
            return None
        if "active_ingredient" not in adapter.products_df.columns:
            return None

        prod = adapter.products_df[
            adapter.products_df["active_ingredient"].str.lower().str.strip()
            == active_name.lower().strip()
        ]
        if prod.empty:
            return None

        codes = set(prod["item_code"])
        names = set(prod["item_name"]) if "item_name" in prod else set()
        is_act = valid_items["item_code"].isin(codes) | valid_items[
            "item_name"
        ].isin(names)
        rev = valid_items[is_act]["amount"].sum()
        if rev == 0:
            return None
        return format_number(rev, is_currency=True)

    @staticmethod
    def calc_top_debt_customers(
        adapter: BusinessAdapter, as_of_date: str, k: int = 3
    ) -> Optional[Dict[str, Any]]:
        df = adapter.orders_df
        if df is None or df.empty:
            return None

        df = df[~df["status"].apply(is_cancelled_status)]
        df = df[
            ~df["payment_method"]
            .str.lower()
            .str.contains("cod|sàn|shopee|lazada|tiki|tiktok", na=False)
        ]
        if as_of_date:
            df = df[df["date"] <= as_of_date]

        df = df[df["debt"] > 0]
        df = df[
            ~df["cust_name"].str.lower().str.contains("khách lẻ|khach le|^$", na=False)
        ]
        if df.empty:
            return None

        grouped = df.groupby(["cust_id", "cust_name"])["debt"].sum().reset_index()
        grouped = grouped.sort_values(
            by=["debt", "cust_id"], ascending=[False, True], kind="mergesort"
        )

        rows = []
        for _, r in grouped.head(k).iterrows():
            rows.append([r["cust_name"], int(round(r["debt"]))])

        if not rows:
            return None
        return format_table(["khach_hang", "so_tien_no"], rows)

    @staticmethod
    def calc_unsold_items(
        adapter: BusinessAdapter, time_info: Dict[str, Any], as_of_date: str
    ) -> Optional[List[str]]:
        if adapter.items_df is None or adapter.items_df.empty:
            return None

        start_d = time_info["start_date"]
        end_d = time_info["end_date"]

        before_items = filter_items_in_period(
            adapter.items_df, "2000-01-01", start_d, as_of_date
        )
        before_items = before_items[before_items["date"] < start_d]
        before_items = before_items[
            ~before_items["item_name"].str.lower().str.contains("phí phục vụ", na=False)
        ]
        before_codes = set(before_items["item_code"])

        curr_items = filter_items_in_period(
            adapter.items_df, start_d, end_d, as_of_date
        )
        curr_codes = set(curr_items["item_code"])

        unsold_codes = before_codes - curr_codes
        if not unsold_codes:
            return []

        if adapter.products_df is not None and not adapter.products_df.empty:
            sub_p = adapter.products_df[
                adapter.products_df["item_code"].isin(unsold_codes)
            ].sort_values("item_code")
            if not sub_p.empty:
                names = sub_p["item_name"].tolist()
            else:
                names = (
                    before_items[before_items["item_code"].isin(unsold_codes)]
                    .drop_duplicates("item_code")
                    .sort_values("item_code")["item_name"]
                    .tolist()
                )
        else:
            names = (
                before_items[before_items["item_code"].isin(unsold_codes)]
                .drop_duplicates("item_code")
                .sort_values("item_code")["item_name"]
                .tolist()
            )

        clean = []
        for n in names:
            n_str = str(n).strip()
            nl = n_str.lower()
            if nl in [
                "khách lẻ",
                "khach le",
                "nan",
                "none",
                "",
                "null",
                "tong cong",
                "tổng cộng",
                "đơn hàng",
            ]:
                continue
            if nl.startswith(("ord", "hd_", "hđ_")):
                continue
            clean.append(n_str)
        return clean

    @staticmethod
    def calc_item_qty_sold(
        adapter: BusinessAdapter,
        time_info: Dict[str, Any],
        as_of_date: str,
        item_query: str,
        unit_name: Optional[str] = None,
    ) -> Optional[Any]:
        valid_items = filter_items_in_period(
            adapter.items_df,
            time_info["start_date"],
            time_info["end_date"],
            as_of_date,
        )
        if valid_items.empty:
            return None
        matches = match_item_rows(valid_items, item_query)
        if matches.empty:
            return None

        total_qty = matches["qty_base"].sum()
        if total_qty == 0:
            return None
        return format_number(total_qty, is_currency=False)

    @staticmethod
    def calc_avg_import_price(
        adapter: BusinessAdapter,
        time_info: Dict[str, Any],
        as_of_date: str,
        item_query: str,
    ) -> Optional[int]:
        if adapter.imports_df is None or adapter.imports_df.empty:
            return None
        df = adapter.imports_df.copy()
        start_d = time_info["start_date"]
        end_d = time_info["end_date"]
        if as_of_date:
            df = df[df["date"] <= as_of_date]
        if start_d:
            df = df[df["date"] >= start_d]
        if end_d:
            df = df[df["date"] <= end_d]
        df = df[~df["status"].apply(is_cancelled_status)]

        matches = match_item_rows(df, item_query)
        if matches.empty:
            return None

        tot_amt = matches["total_amount"].sum()
        tot_qty = matches["qty"].sum()
        if tot_qty == 0 or tot_amt == 0:
            return None
        avg_price = tot_amt / tot_qty
        return format_number(avg_price, is_currency=True)

    @staticmethod
    def calc_item_revenue(
        adapter: BusinessAdapter,
        time_info: Dict[str, Any],
        as_of_date: str,
        item_query: str,
    ) -> Optional[int]:
        valid_items = filter_items_in_period(
            adapter.items_df,
            time_info["start_date"],
            time_info["end_date"],
            as_of_date,
        )
        if valid_items.empty:
            return None
        matches = match_item_rows(valid_items, item_query)
        if matches.empty:
            return None
        total_rev = matches["amount"].sum()
        if total_rev == 0:
            return None
        return format_number(total_rev, is_currency=True)

    @staticmethod
    def calc_monthly_revenue_list(
        adapter: BusinessAdapter, periods: List[Dict[str, Any]], as_of_date: str
    ) -> Optional[List[int]]:
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
    def calc_highest_revenue_month(
        adapter: BusinessAdapter, periods: List[Dict[str, Any]], as_of_date: str
    ) -> Optional[str]:
        if not periods:
            return None
        best_label = None
        best_rev = -1
        for p in periods:
            r = QueryExecutors.calc_revenue(adapter, p, as_of_date)
            if r is not None and r > best_rev:
                best_rev = r
                best_label = p.get("month_label") or p.get("label")
        return best_label if best_rev > 0 else None

    @staticmethod
    def calc_days_above_revenue(
        adapter: BusinessAdapter,
        time_info: Dict[str, Any],
        as_of_date: str,
        min_revenue: float,
    ) -> Optional[int]:
        valid_items = filter_items_in_period(
            adapter.items_df,
            time_info["start_date"],
            time_info["end_date"],
            as_of_date,
        )
        if valid_items.empty or min_revenue <= 0:
            return None
        daily = valid_items.groupby("date")["amount"].sum()
        cnt = int((daily >= min_revenue).sum())
        return cnt if cnt > 0 else None

    @staticmethod
    def calc_item_unit_price(
        adapter: BusinessAdapter,
        time_info: Dict[str, Any],
        as_of_date: str,
        item_query: str,
    ) -> Optional[int]:
        valid_items = filter_items_in_period(
            adapter.items_df,
            time_info["start_date"],
            time_info["end_date"],
            as_of_date,
        )
        if valid_items.empty:
            return None
        matches = match_item_rows(valid_items, item_query)
        if matches.empty:
            return None
        pos = matches[matches["unit_price"] > 0]
        if pos.empty:
            return None
        u_price = pos["unit_price"].min()
        return format_number(u_price, is_currency=True)

    @staticmethod
    def calc_top_customers(
        adapter: BusinessAdapter,
        time_info: Dict[str, Any],
        as_of_date: str,
        k: int = 1,
    ) -> Optional[Any]:
        valid_items = filter_items_in_period(
            adapter.items_df,
            time_info["start_date"],
            time_info["end_date"],
            as_of_date,
        )
        if valid_items.empty or "cust_name" not in valid_items.columns:
            return None
        df = valid_items.copy()
        df["cust_name_clean"] = df["cust_name"].fillna("").astype(str).str.strip()
        df = df[
            ~df["cust_name_clean"]
            .str.lower()
            .isin(["", "nan", "none", "khách lẻ", "khach le", "khách vãng lai"])
        ]
        if df.empty:
            return None
        df["cust_id_clean"] = (
            df["cust_id"].fillna("").astype(str).str.strip()
            if "cust_id" in df.columns
            else df["cust_name_clean"]
        )
        df["cust_id_clean"] = [
            cid if cid and cid.lower() not in ("nan", "none") else cname
            for cid, cname in zip(df["cust_id_clean"], df["cust_name_clean"])
        ]
        grouped = (
            df.groupby(["cust_id_clean", "cust_name_clean"])["amount"]
            .sum()
            .reset_index()
        )
        grouped = grouped.sort_values(
            by=["amount", "cust_id_clean"], ascending=[False, True], kind="mergesort"
        )
        top_names = grouped["cust_name_clean"].head(k).tolist()
        if not top_names:
            return None
        return top_names if k > 1 else top_names[0]


@dataclass
class SandboxResult:
    ok: bool
    value: Any = None
    error_type: Optional[str] = None
    traceback: Optional[str] = None
    stdout: str = ""
    elapsed: float = 0.0
    unanswerable_reason: Optional[str] = None


def extract_code(llm_text: str) -> Optional[str]:
    """Trích xuất khối mã Python từ phản hồi của Qwen."""
    if not llm_text:
        return None
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", llm_text, re.DOTALL)
    if blocks:
        return blocks[-1].strip()
    if "final_result" in llm_text:
        lines = []
        for line in llm_text.splitlines():
            if line.strip().startswith(("<plan>", "</plan>", "```")):
                continue
            lines.append(line)
        return "\n".join(lines).strip()
    return None


def static_check(code: str) -> List[str]:
    """
    Kiểm tra tĩnh AST (AST Security Whitelist):
    - Cấm truy cập hệ thống file (open, to_csv, to_excel, os, sys, subprocess, socket, eval, exec).
    - Cho phép import pandas, numpy, math, re, datetime (hoặc dùng biến đã nạp sẵn).
    - Bắt buộc mã phải gán biến `final_result`.
    """
    violations: List[str] = []
    if not code or not code.strip():
        return ["Đoạn mã rỗng"]

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"SyntaxError tại dòng {e.lineno}: {e.msg}"]

    allowed_imports = {"pandas", "numpy", "math", "re", "datetime", "collections", "decimal"}
    forbidden_calls = {
        "open",
        "exec",
        "eval",
        "compile",
        "__import__",
        "input",
        "breakpoint",
        "exit",
        "quit",
        "system",
        "popen",
    }
    forbidden_attrs = {
        "to_csv",
        "to_excel",
        "to_pickle",
        "to_parquet",
        "to_sql",
        "read_csv",
        "read_excel",
        "read_sql",
        "read_pickle",
    }

    has_final_result = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_mod = alias.name.split(".")[0]
                if root_mod not in allowed_imports:
                    violations.append(f"Không được phép import module '{alias.name}'")
        elif isinstance(node, ast.ImportFrom):
            root_mod = (node.module or "").split(".")[0]
            if root_mod not in allowed_imports:
                violations.append(f"Không được phép import từ '{node.module}'")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in forbidden_calls:
                violations.append(f"Hàm bị cấm trong Sandbox: '{node.func.id}()'")
            elif isinstance(node.func, ast.Attribute) and node.func.attr in forbidden_attrs:
                violations.append(
                    f"Phương thức I/O bị cấm trong Sandbox: '.{node.func.attr}()'"
                )
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr not in ("__len__", "__getitem__"):
                violations.append(f"Không được phép truy cập thuộc tính dunder '{node.attr}'")
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "final_result":
                    has_final_result = True
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "final_result":
                has_final_result = True

    if not has_final_result:
        violations.append("Mã bắt buộc phải gán kết quả cuối cùng vào biến `final_result`.")

    return violations


def build_sandbox_globals(
    bundle: ShopBundle,
    tctx: TimeContext,
    ents: EntityResult,
    cal: Optional[CalendarResolver],
    as_of_date: str,
) -> Dict[str, Any]:
    """
    Khởi tạo môi trường biến toàn cục & hàm Helper chuẩn Rulebook cho Sandbox:
    - V: dict các bảng chuẩn hóa ('sales', 'orders', 'products', 'imports', 'expenses', 'inventory', 'customers')
    - dfs: dict các bảng gốc
    - T: dict thời gian đã tính sẵn ('start', 'end', 'cmp_start', 'cmp_end', 'shift', 'periods', 'as_of')
    - E: dict ứng viên thực thể đã tìm thấy
    - Các hàm helper: revenue_lines, item_lines, valid_orders, all_orders, revenue, money, pct, growth, diff, top_k, match_item, UNANSWERABLE
    """
    t_dict = tctx.to_legacy_dict()
    e_dict = {
        k: [
            {"code": c.code, "name": c.name, "score": c.score}
            for c in v
        ]
        for k, v in ents.candidates.items()
    }

    def _revenue_lines(
        start: Optional[str] = t_dict["start"],
        end: Optional[str] = t_dict["end"],
        shift: Optional[str] = t_dict["shift"],
    ) -> pd.DataFrame:
        return filter_items_in_period(
            bundle.adapter.items_df, start, end, as_of_date, shift
        )

    def _item_lines(
        start: Optional[str] = t_dict["start"],
        end: Optional[str] = t_dict["end"],
        shift: Optional[str] = t_dict["shift"],
    ) -> pd.DataFrame:
        df = filter_items_in_period(
            bundle.adapter.items_df, start, end, as_of_date, shift
        )
        if not df.empty and "is_service_fee" in df.columns:
            df = df[~df["is_service_fee"]]
        elif not df.empty and "item_name" in df.columns:
            df = df[~df["item_name"].str.lower().str.contains("phí phục vụ", na=False)]
        return df

    def _valid_orders(
        start: Optional[str] = t_dict["start"],
        end: Optional[str] = t_dict["end"],
        shift: Optional[str] = t_dict["shift"],
    ) -> pd.DataFrame:
        lines = _revenue_lines(start, end, shift)
        if lines.empty:
            return pd.DataFrame(columns=["order_id", "date", "cust_key", "cust_name", "amount"])
        return (
            lines.groupby(["order_id", "date", "cust_key", "cust_name"], as_index=False)["amount"]
            .sum()
        )

    def _all_orders(
        start: Optional[str] = t_dict["start"],
        end: Optional[str] = t_dict["end"],
    ) -> pd.DataFrame:
        odf = bundle.adapter.orders_df
        if odf is None or odf.empty:
            if bundle.adapter.items_df is None or bundle.adapter.items_df.empty:
                return pd.DataFrame()
            odf = bundle.adapter.items_df[["order_id", "date", "status", "status_norm"]].drop_duplicates("order_id")
        sub = odf.copy()
        if as_of_date and "date" in sub.columns:
            sub = sub[sub["date"] <= as_of_date]
        if start and "date" in sub.columns:
            sub = sub[sub["date"] >= start]
        if end and "date" in sub.columns:
            sub = sub[sub["date"] <= end]
        return sub

    def _revenue(
        start: Optional[str] = t_dict["start"],
        end: Optional[str] = t_dict["end"],
        shift: Optional[str] = t_dict["shift"],
    ) -> int:
        lines = _revenue_lines(start, end, shift)
        if lines.empty:
            return 0
        return int(math.floor(float(lines["amount"].sum()) + 0.5))

    def _money(x: Any) -> int:
        return int(math.floor(float(x) + 0.5))

    def _pct(num: float, den: float) -> float:
        if den == 0:
            raise ZeroDivisionError("Mẫu số bằng 0 khi tính phần trăm")
        return round_half_up((float(num) / float(den)) * 100.0, 2)

    def _growth(curr: float, prev: float) -> float:
        if prev == 0:
            raise ZeroDivisionError("Giá trị kỳ trước bằng 0 khi tính tăng trưởng %")
        return round_half_up(((float(curr) - float(prev)) / float(prev)) * 100.0, 2)

    def _diff(curr: float, prev: float) -> Any:
        return curr - prev

    def _top_k(
        df: pd.DataFrame,
        value_col: str = "qty_base",
        k: int = 1,
        key_col: str = "item_code",
        name_col: str = "item_name",
        ascending: bool = False,
    ) -> Any:
        if df is None or df.empty:
            return [] if k > 1 else None
        grouped = df.groupby([key_col, name_col], as_index=False)[value_col].sum()
        grouped = grouped.sort_values(
            by=[value_col, key_col], ascending=[ascending, True], kind="mergesort"
        )
        names = [
            str(n).strip()
            for n in grouped[name_col].tolist()
            if str(n).strip() and str(n).strip().lower() not in ("nan", "none", "khách lẻ", "đơn hàng")
        ][:k]
        if not names:
            return None
        return names[0] if k == 1 else names

    def _match_item(df: pd.DataFrame, query: str) -> pd.DataFrame:
        return match_item_rows(df, query)

    def _unanswerable(reason: str = "Dữ liệu không có thông tin này") -> Dict[str, str]:
        return {UNANSWERABLE_SENTINEL: str(reason)}

    def _count_weekday(start_s: str, end_s: str, iso_wd: int) -> int:
        if cal:
            return cal.count_weekday(start_s, end_s, iso_wd)
        d = datetime.strptime(start_s, "%Y-%m-%d").date()
        e = datetime.strptime(end_s, "%Y-%m-%d").date()
        c = 0
        while d <= e:
            if d.isoweekday() == iso_wd:
                c += 1
            d += timedelta(days=1)
        return c

    safe_builtins = {
        "abs": abs,
        "all": all,
        "any": any,
        "bool": bool,
        "dict": dict,
        "enumerate": enumerate,
        "filter": filter,
        "float": float,
        "int": int,
        "isinstance": isinstance,
        "len": len,
        "list": list,
        "map": map,
        "max": max,
        "min": min,
        "print": print,
        "range": range,
        "round": round,
        "set": set,
        "sorted": sorted,
        "str": str,
        "sum": sum,
        "tuple": tuple,
        "zip": zip,
        "__import__": __import__,
        "Exception": Exception,
        "ValueError": ValueError,
        "KeyError": KeyError,
        "ZeroDivisionError": ZeroDivisionError,
        "IndexError": IndexError,
        "TypeError": TypeError,
    }

    return {
        "__builtins__": safe_builtins,
        "pd": pd,
        "np": np,
        "math": math,
        "re": re,
        "date": date,
        "datetime": datetime,
        "timedelta": timedelta,
        "V": bundle.views,
        "dfs": bundle.dfs,
        "T": t_dict,
        "E": e_dict,
        "as_of_date": as_of_date,
        "filter_items_in_period": filter_items_in_period,
        "revenue_lines": _revenue_lines,
        "item_lines": _item_lines,
        "valid_orders": _valid_orders,
        "all_orders": _all_orders,
        "revenue": _revenue,
        "money": _money,
        "pct": _pct,
        "growth": _growth,
        "diff": _diff,
        "top_k": _top_k,
        "match_item": _match_item,
        "count_weekday": _count_weekday,
        "UNANSWERABLE": _unanswerable,
        "clean_currency_str": clean_currency_str,
        "strip_accents": strip_accents,
    }


def shorten_traceback(
    tb_str: str, code: str, bundle: ShopBundle, max_len: int = 900
) -> str:
    """
    Rút gọn Traceback lỗi từ Sandbox và đính kèm gợi ý cột có thật trong Schema
    để Qwen sửa code chính xác ở Vòng 2 (Self-Correction).
    """
    lines = [l for l in tb_str.strip().splitlines() if l.strip()]
    err_line = lines[-1] if lines else "RuntimeError"

    # Tìm số dòng trong <llm_code>
    line_info = ""
    for l in reversed(lines):
        m_ln = re.search(r'File "<llm_code>", line (\d+)', l)
        if m_ln:
            ln_no = int(m_ln.group(1))
            code_lines = code.splitlines()
            bad_src = (
                code_lines[ln_no - 1].strip()
                if 1 <= ln_no <= len(code_lines)
                else ""
            )
            line_info = f"Dòng lỗi {ln_no}: `{bad_src}`\n"
            break

    hint = ""
    if "KeyError" in err_line:
        v_info = []
        for k, vdf in bundle.views.items():
            v_info.append(f"V['{k}'] có các cột: {list(vdf.columns)}")
        hint = "\nGợi ý Schema chuẩn: " + " | ".join(v_info[:4])

    out = f"{err_line}\n{line_info}{hint}".strip()
    return out[:max_len]


def _fork_worker(code: str, globals_dict: Dict[str, Any], conn: Any, max_stdout: int) -> None:
    """Worker chạy trong tiến trình con khi hệ điều hành hỗ trợ fork (Linux / Colab / Docker)."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            exec(compile(code, "<llm_code>", "exec"), globals_dict)
        if "final_result" not in globals_dict:
            conn.send(
                {
                    "ok": False,
                    "error_type": "NoFinalResult",
                    "tb": "Thiếu biến `final_result` trong đoạn code.",
                    "stdout": buf.getvalue()[:max_stdout],
                }
            )
        else:
            val = to_native(globals_dict["final_result"])
            conn.send(
                {
                    "ok": True,
                    "value": val,
                    "stdout": buf.getvalue()[:max_stdout],
                }
            )
    except BaseException as e:
        conn.send(
            {
                "ok": False,
                "error_type": type(e).__name__,
                "tb": traceback.format_exc(limit=5),
                "stdout": buf.getvalue()[:max_stdout],
            }
        )
    finally:
        conn.close()


class Sandbox:
    """
    Module Thực thi Mã Pandas An toàn (Bước 4 trong Kiến trúc):
    - Kiểm tra AST trước khi thực thi.
    - Giới hạn thời gian chạy (mặc định timeout = 10.0 giây).
    - Tự động dùng multiprocessing 'fork' trên Linux/Docker/Colab (copy-on-write cực nhanh, ngắt cứng vòng lặp vô hạn),
      và dùng Thread + Timeout an toàn trên Windows khi chạy test local.
    - Bắt mọi Exception và trả về Traceback ngắn gọn kèm gợi ý Schema cho vòng Self-Correction.
    """

    def __init__(self, timeout_s: float = 10.0, max_stdout: int = 2000) -> None:
        self.timeout_s = timeout_s
        self.max_stdout = max_stdout

    def execute(
        self,
        code: Optional[str],
        bundle: ShopBundle,
        tctx: TimeContext,
        ents: EntityResult,
        cal: Optional[CalendarResolver],
        as_of_date: str,
    ) -> SandboxResult:
        t0 = time.monotonic()
        if not code or not code.strip():
            return SandboxResult(
                ok=False,
                error_type="EmptyCode",
                traceback="Không tìm thấy khối code Python hợp lệ.",
                elapsed=time.monotonic() - t0,
            )

        violations = static_check(code)
        if violations:
            return SandboxResult(
                ok=False,
                error_type="CodeRejected",
                traceback="; ".join(violations),
                elapsed=time.monotonic() - t0,
            )

        g_dict = build_sandbox_globals(bundle, tctx, ents, cal, as_of_date)

        # Trên Linux / Docker / Colab: sử dụng multiprocessing.get_context('fork')
        if sys.platform != "win32" and "fork" in mp.get_all_start_methods():
            ctx = mp.get_context("fork")
            parent_conn, child_conn = ctx.Pipe(duplex=False)
            proc = ctx.Process(
                target=_fork_worker,
                args=(code, g_dict, child_conn, self.max_stdout),
                daemon=True,
            )
            proc.start()
            child_conn.close()
            if parent_conn.poll(self.timeout_s):
                msg = parent_conn.recv()
                proc.join(0.5)
            else:
                msg = {
                    "ok": False,
                    "error_type": "TimeoutError",
                    "tb": f"Đoạn mã chạy quá giới hạn {self.timeout_s:.0f} giây. Hãy dùng phép toán vector hóa của Pandas thay vì vòng lặp.",
                    "stdout": "",
                }
            if proc.is_alive():
                proc.kill()
                proc.join(0.5)
            parent_conn.close()
        else:
            # Trên Windows (môi trường test local): chạy trong worker thread có giới hạn thời gian
            msg_box: List[Dict[str, Any]] = []

            def _thread_target() -> None:
                buf = io.StringIO()
                try:
                    with contextlib.redirect_stdout(buf):
                        exec(compile(code, "<llm_code>", "exec"), g_dict)
                    if "final_result" not in g_dict:
                        msg_box.append(
                            {
                                "ok": False,
                                "error_type": "NoFinalResult",
                                "tb": "Thiếu biến `final_result`.",
                                "stdout": buf.getvalue()[: self.max_stdout],
                            }
                        )
                    else:
                        msg_box.append(
                            {
                                "ok": True,
                                "value": to_native(g_dict["final_result"]),
                                "stdout": buf.getvalue()[: self.max_stdout],
                            }
                        )
                except BaseException as e:
                    msg_box.append(
                        {
                            "ok": False,
                            "error_type": type(e).__name__,
                            "tb": traceback.format_exc(limit=5),
                            "stdout": buf.getvalue()[: self.max_stdout],
                        }
                    )

            th = threading.Thread(target=_thread_target, daemon=True)
            th.start()
            th.join(self.timeout_s)
            if th.is_alive() or not msg_box:
                msg = {
                    "ok": False,
                    "error_type": "TimeoutError",
                    "tb": f"Đoạn mã chạy quá giới hạn {self.timeout_s:.0f} giây.",
                    "stdout": "",
                }
            else:
                msg = msg_box[0]

        elapsed = time.monotonic() - t0
        if not msg.get("ok"):
            short_tb = shorten_traceback(msg.get("tb", ""), code, bundle)
            return SandboxResult(
                ok=False,
                error_type=msg.get("error_type", "RuntimeError"),
                traceback=short_tb,
                stdout=msg.get("stdout", ""),
                elapsed=elapsed,
            )

        val = msg.get("value")
        if isinstance(val, dict) and UNANSWERABLE_SENTINEL in val:
            return SandboxResult(
                ok=True,
                value=None,
                unanswerable_reason=str(val[UNANSWERABLE_SENTINEL]),
                stdout=msg.get("stdout", ""),
                elapsed=elapsed,
            )

        return SandboxResult(
            ok=True,
            value=val,
            stdout=msg.get("stdout", ""),
            elapsed=elapsed,
        )

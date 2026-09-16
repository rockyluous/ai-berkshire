#!/usr/bin/env python3
"""usstock_data.py 离线回归测试（不联网，用合成的 companyfacts 片段）。

覆盖：
  - 同一期末多次申报时取最新 filed（重述后的值）
  - Q4 = 年度 − 前三季 YTD 的推算，且带 derived 标记
  - 财年/季度标签按期末日生成（非日历财年公司也正确）
  - 多类别股票封面页股数加总
  - 非经营损益占比预警阈值

运行：  python3 tests/test_usstock_data.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools'))

import usstock_data as U  # noqa: E402


def _dur(start, end, val, filed, form="10-Q", frame=None):
    r = {"start": start, "end": end, "val": val, "filed": filed, "form": form, "fy": 2026, "fp": "Q2"}
    if frame:
        r["frame"] = frame
    return r


def _inst(end, val, filed, form="10-Q"):
    return {"end": end, "val": val, "filed": filed, "form": form, "fy": 2026, "fp": "Q2"}


def _facts(revenue_rows, extra=None):
    gaap = {"Revenues": {"units": {"USD": revenue_rows}}}
    if extra:
        gaap.update(extra)
    return {"facts": {"us-gaap": gaap, "dei": {}}}


class TestSeriesDedup(unittest.TestCase):
    def test_latest_filing_wins(self):
        rows = [
            _dur("2025-01-01", "2025-12-31", 100, "2026-02-01", "10-K"),
            _dur("2025-01-01", "2025-12-31", 101, "2026-07-20", "10-Q"),  # 重述
        ]
        self.assertEqual(U.annual_series(rows), {"2025-12-31": 101})

    def test_duration_windows(self):
        rows = [
            _dur("2025-01-01", "2025-12-31", 400, "2026-02-01", "10-K"),   # 年
            _dur("2025-01-01", "2025-09-30", 300, "2025-10-20"),            # 9M YTD
            _dur("2025-07-01", "2025-09-30", 110, "2025-10-20"),            # Q3
        ]
        self.assertEqual(U.annual_series(rows), {"2025-12-31": 400})
        q, derived = U.quarter_series(rows)
        self.assertEqual(q["2025-09-30"], 110)


class TestQ4Derivation(unittest.TestCase):
    def test_q4_equals_fy_minus_ytd9(self):
        rows = [
            _dur("2025-01-01", "2025-03-31", 90, "2025-04-20"),
            _dur("2025-04-01", "2025-06-30", 100, "2025-07-20"),
            _dur("2025-07-01", "2025-09-30", 110, "2025-10-20"),
            _dur("2025-01-01", "2025-09-30", 300, "2025-10-20"),
            _dur("2025-01-01", "2025-12-31", 420, "2026-02-01", "10-K"),
        ]
        q, derived = U.quarter_series(rows)
        self.assertEqual(q["2025-12-31"], 120)
        self.assertIn("2025-12-31", derived)
        self.assertNotIn("2025-09-30", derived)

    def test_build_quarterly_marks_derived(self):
        rows = [
            _dur("2025-01-01", "2025-09-30", 300, "2025-10-20"),
            _dur("2025-07-01", "2025-09-30", 110, "2025-10-20"),
            _dur("2025-01-01", "2025-12-31", 420, "2026-02-01", "10-K"),
        ]
        out = U.build_quarterly(_facts(rows), n=8)
        by = {r["period"]: r for r in out}
        self.assertTrue(by["2025Q4"]["derived_q4"])
        self.assertFalse(by["2025Q3"]["derived_q4"])
        self.assertEqual(by["2025Q4"]["revenue"], 120)


class TestLabels(unittest.TestCase):
    def test_fiscal_year_from_end_date(self):
        self.assertEqual(U.fy_label("2025-09-27"), "FY2025")   # Apple 式财年
        self.assertEqual(U.fy_label("2026-01-25"), "FY2026")   # Nvidia 式财年

    def test_quarter_label(self):
        self.assertEqual(U.q_label("2026-06-30"), "2026Q2")
        self.assertEqual(U.q_label("2025-12-31"), "2025Q4")


class TestSharesOutstanding(unittest.TestCase):
    def test_multi_class_summed(self):
        facts = {"facts": {"us-gaap": {}, "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
            {"end": "2026-07-15", "val": 5.8e9, "filed": "2026-07-23", "frame": "CY2026Q2I"},
            {"end": "2026-07-15", "val": 0.8e9, "filed": "2026-07-23"},
            {"end": "2026-07-15", "val": 5.6e9, "filed": "2026-07-23"},
            {"end": "2026-04-15", "val": 12.1e9, "filed": "2026-04-25"},   # 旧的一期，不应混入
        ]}}}}}
        total, end, note = U.shares_outstanding(facts)
        self.assertAlmostEqual(total, 12.2e9)
        self.assertEqual(end, "2026-07-15")
        self.assertIn("多类别", note)


class TestOneOffWarning(unittest.TestCase):
    def _rows(self, non):
        return [{"nonoperating": n, "operating_income": 400} for n in non]

    def test_warns_when_nonoperating_large(self):
        self.assertIsNotNone(U._oneoff_warning(self._rows([10, 20, 287, 980])))

    def test_silent_when_small(self):
        self.assertIsNone(U._oneoff_warning(self._rows([10, 12, 9, 11])))


class TestAnnualBuild(unittest.TestCase):
    def test_roe_uses_average_equity(self):
        rev = [_dur("2024-01-01", "2024-12-31", 1000, "2025-02-01", "10-K"),
               _dur("2025-01-01", "2025-12-31", 1200, "2026-02-01", "10-K")]
        extra = {
            "NetIncomeLoss": {"units": {"USD": [
                _dur("2024-01-01", "2024-12-31", 100, "2025-02-01", "10-K"),
                _dur("2025-01-01", "2025-12-31", 150, "2026-02-01", "10-K")]}},
            "StockholdersEquity": {"units": {"USD": [
                _inst("2024-12-31", 800, "2025-02-01", "10-K"),
                _inst("2025-12-31", 1200, "2026-02-01", "10-K")]}},
        }
        out = U.build_annual(_facts(rev, extra), years=5)
        self.assertEqual([r["period"] for r in out], ["FY2024", "FY2025"])
        self.assertAlmostEqual(out[1]["roe"], 150 / 1000)     # 平均权益 (800+1200)/2
        self.assertAlmostEqual(out[0]["roe"], 100 / 800)      # 首年无上年权益 → 期末权益


if __name__ == '__main__':
    unittest.main(verbosity=1)

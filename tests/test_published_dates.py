"""발행일 파서(parse_published) — KST 변환, 신뢰도, 모호성, 경계, 윤일, 연말 주차, 필터 정책."""

import unittest
from datetime import date

from content_quality import filter_items, make_index_entry
from dates import WeekPeriod, date_failure_reason, parse_published

W3 = WeekPeriod(year=2026, week_label="2026-09-W3", start=date(2026, 9, 21), end=date(2026, 9, 27))
YEAR_END = WeekPeriod(year=2026, week_label="2026-12-W4", start=date(2026, 12, 28), end=date(2027, 1, 3))

CASES = [
    # (입력, KST 날짜, 신뢰도, tz)
    ("2026-09-23", date(2026, 9, 23), "high", "none"),
    ("2026.9.23.", date(2026, 9, 23), "high", "none"),
    ("2026/9/23", date(2026, 9, 23), "high", "none"),
    ("2026년 9월 23일", date(2026, 9, 23), "high", "none"),
    ("2026-09-23 (수)", date(2026, 9, 23), "high", "none"),
    ("2026-09-23T23:30:00Z", date(2026, 9, 24), "high", "explicit"),
    ("2026-09-23T15:30:00+00:00", date(2026, 9, 24), "high", "explicit"),
    ("2026-09-23T10:00:00+09:00", date(2026, 9, 23), "high", "explicit"),
    ("2026-09-23T10:00:00-05:00", date(2026, 9, 24), "high", "explicit"),
    ("2026-09-23T23:30:00 KST", date(2026, 9, 23), "high", "explicit"),
    ("Wed, 23 Sep 2026 15:30:00 GMT", date(2026, 9, 24), "high", "explicit"),
    ("23 Sep 2026 10:00:00 +0900", date(2026, 9, 23), "high", "explicit"),
    ("2026-09-23 14:00", date(2026, 9, 23), "medium", "assumed_kst"),
    ("2026년 9월 23일 오후 11시", date(2026, 9, 23), "medium", "assumed_kst"),
    ("Updated 2026 09 23 (v3)", date(2026, 9, 23), "low", "none"),
]

UNPARSEABLE = [
    ("9/23/2026", "date_ambiguous"),
    ("23.09.2026", "date_ambiguous"),
    ("01/02/2026", "date_ambiguous"),
    ("20260923", "date_unparseable"),
    ("2026-09", "date_unparseable"),
    ("2026", "date_unparseable"),
    ("2026-02-30", "date_unparseable"),
    ("2027-02-29", "date_unparseable"),
    ("2026-09-23 24:30", "date_unparseable"),
    ("2026-09-23 25:00", "date_unparseable"),
    ("not a date", "date_unparseable"),
    ("", "date_missing"),
    (None, "date_missing"),
    (20260923, "date_unparseable"),
]


class ParsePublishedTests(unittest.TestCase):
    def test_parsed_values_confidence_and_tz(self):
        for raw, d, conf, tz in CASES:
            with self.subTest(raw=raw):
                pd = parse_published(raw)
                self.assertIsNotNone(pd)
                self.assertEqual((pd.date, pd.confidence, pd.tz), (d, conf, tz))

    def test_unparseable_and_reasons(self):
        for raw, reason in UNPARSEABLE:
            with self.subTest(raw=raw):
                self.assertIsNone(parse_published(raw))
                self.assertEqual(date_failure_reason(raw), reason)

    def test_t24_is_next_day_midnight(self):
        self.assertEqual(parse_published("2026-09-23 24:00").date, date(2026, 9, 24))
        self.assertEqual(parse_published("2026-09-23T24:00:00+09:00").date, date(2026, 9, 24))
        # 24:00Z = 다음 날 00:00Z = 다음 날 09:00 KST
        self.assertEqual(parse_published("2026-09-23T24:00:00Z").date, date(2026, 9, 24))

    def test_leap_day_and_year_end(self):
        self.assertEqual(parse_published("2028-02-29").date, date(2028, 2, 29))
        self.assertEqual(parse_published("2026-12-31T16:00:00Z").date, date(2027, 1, 1))
        self.assertEqual(parse_published("2026-12-31T14:59:59Z").date, date(2026, 12, 31))

    def test_week_boundaries_in_kst(self):
        # UTC 일요일 14:59:59 = KST 일요일 23:59:59 (해당 주), 15:00:00 = KST 월요일 00:00 (다음 주)
        in_week = parse_published("2026-09-27T14:59:59Z").date
        next_week = parse_published("2026-09-27T15:00:00Z").date
        self.assertTrue(W3.contains(in_week))
        self.assertFalse(W3.contains(next_week))
        # 월요일 00:00 KST (= 일요일 15:00Z) 직전/직후
        self.assertTrue(W3.contains(parse_published("2026-09-21T00:00:00+09:00").date))
        self.assertFalse(W3.contains(parse_published("2026-09-20T23:59:59+09:00").date))

    def test_year_end_week_contains_new_year(self):
        self.assertTrue(YEAR_END.contains(parse_published("2027-01-01").date))
        self.assertTrue(YEAR_END.contains(parse_published("2026-12-31T16:00:00Z").date))
        self.assertFalse(YEAR_END.contains(parse_published("2027-01-03T15:00:00Z").date))  # KST 1/4 월요일


class FilterPolicyTests(unittest.TestCase):
    def _filter(self, date_value, index=None):
        log = []
        item = {"title": "충분히 긴 테스트 기사 제목", "summary": "s", "source": "m",
                "source_url": "", "date": date_value}
        kept = filter_items([item], W3, index if index is not None else [], "architecture", "domestic", log,
                            verify_urls=False)
        return kept, log

    def test_low_confidence_date_is_excluded_from_week_decision(self):
        kept, log = self._filter("Updated 2026 09 23 (v3)")
        self.assertEqual(kept, [])
        self.assertEqual(log[0]["reason"], "date_low_confidence")
        self.assertEqual(log[0]["candidate_date"], "2026-09-23")

    def test_ambiguous_missing_unparseable_reasons(self):
        for value, reason in (("9/23/2026", "date_ambiguous"), ("", "date_missing"), ("abc", "date_unparseable")):
            with self.subTest(value=value):
                kept, log = self._filter(value)
                self.assertEqual((kept, log[0]["reason"]), ([], reason))

    def test_timezone_aware_dates_are_converted_before_week_check(self):
        kept, _ = self._filter("2026-09-27T14:59:59Z")
        self.assertEqual(len(kept), 1)
        kept, log = self._filter("2026-09-27T15:00:00Z")
        self.assertEqual((kept, log[0]["reason"]), ([], "date_out_of_range"))

    def test_medium_confidence_is_accepted(self):
        kept, _ = self._filter("2026-09-23 14:00")
        self.assertEqual(len(kept), 1)

    def test_low_confidence_date_is_not_used_for_proximity(self):
        low = make_index_entry("충분히 긴 테스트 기사 제목", "", "Updated 2026 09 23 (v3)")
        high = make_index_entry("충분히 긴 테스트 기사 제목", "", "2026-09-23")
        self.assertIsNone(low["date"])
        self.assertEqual(high["date"], date(2026, 9, 23))


if __name__ == "__main__":
    unittest.main()

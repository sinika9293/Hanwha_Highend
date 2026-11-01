import unittest
from datetime import date, datetime, timedelta, timezone

import dates
from dates import (
    KST,
    get_last_completed_week_period,
    get_next_month_period,
    get_previous_month_period,
    today_kst,
)


class WeekBoundaryTests(unittest.TestCase):
    def test_monday_is_next_week_start(self):
        p = get_last_completed_week_period(date(2026, 9, 28))  # 월
        self.assertEqual((p.start, p.end), (date(2026, 9, 21), date(2026, 9, 27)))
        self.assertEqual(p.week_label, "2026-09-W3")

    def test_sunday_still_previous_completed_week(self):
        p = get_last_completed_week_period(date(2026, 9, 27))  # 일
        self.assertEqual((p.start, p.end), (date(2026, 9, 14), date(2026, 9, 20)))

    def test_half_open_range(self):
        p = get_last_completed_week_period(date(2026, 9, 28))
        self.assertTrue(p.contains(date(2026, 9, 21)))
        self.assertTrue(p.contains(date(2026, 9, 27)))
        self.assertFalse(p.contains(date(2026, 9, 20)))
        self.assertFalse(p.contains(date(2026, 9, 28)))
        self.assertEqual(p.end_exclusive, date(2026, 9, 28))

    def test_year_end_to_new_year(self):
        p = get_last_completed_week_period(date(2027, 1, 4))  # 월
        self.assertEqual((p.start, p.end), (date(2026, 12, 28), date(2027, 1, 3)))
        self.assertEqual(p.week_label, "2026-12-W4")
        self.assertTrue(p.contains(date(2027, 1, 1)))
        self.assertFalse(p.contains(date(2027, 1, 4)))

    def test_monthly_year_rollover(self):
        self.assertEqual(get_previous_month_period(date(2027, 1, 15)).label, "2026-12")
        self.assertEqual(get_next_month_period(date(2026, 12, 15)).label, "2027-01")


class KstTests(unittest.TestCase):
    def setUp(self):
        self._orig = dates.datetime

    def tearDown(self):
        dates.datetime = self._orig

    def _freeze_utc(self, utc_dt):
        class Fake(datetime):
            @classmethod
            def now(cls, tz=None):
                return utc_dt.astimezone(tz) if tz else utc_dt

        dates.datetime = Fake

    def test_utc_sunday_evening_is_kst_monday(self):
        self._freeze_utc(datetime(2026, 9, 27, 15, 0, tzinfo=timezone.utc))  # KST 월 00:00
        self.assertEqual(today_kst(), date(2026, 9, 28))
        p = get_last_completed_week_period()
        self.assertEqual(p.week_label, "2026-09-W3")

    def test_utc_sunday_1459_is_still_kst_sunday(self):
        self._freeze_utc(datetime(2026, 9, 27, 14, 59, tzinfo=timezone.utc))
        self.assertEqual(today_kst(), date(2026, 9, 27))
        self.assertEqual(get_last_completed_week_period().week_label, "2026-09-W2")

    def test_monthly_uses_kst(self):
        self._freeze_utc(datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc))  # KST 10/1 00:00
        self.assertEqual(get_previous_month_period().label, "2026-09")
        self.assertEqual(get_next_month_period().label, "2026-11")

    def test_kst_offset(self):
        self.assertEqual(KST.utcoffset(None), timedelta(hours=9))


if __name__ == "__main__":
    unittest.main()

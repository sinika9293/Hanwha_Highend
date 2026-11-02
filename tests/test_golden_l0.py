"""L0 URL 정규화(`content_quality.normalize_url`)와 레거시 `parse_date`의 현재 출력을 고정하는 골든 테스트.

P3에서 날짜 파서·article_key(L1)를 추가해도 L0는 한 글자도 바뀌면 안 된다.
기대값은 P3 착수 전(기준 커밋 b808491)의 실제 출력이다.
"""

import unittest
from datetime import date

from content_quality import normalize_url, parse_date

L0_GOLDEN = [
    ("HTTP://WWW.Example.com:443/Path/?utm_source=x&b=1#f", "http://example.com:443/Path?b=1"),
    ("https://example.com:443/x", "https://example.com:443/x"),
    ("http://example.com:80/x", "http://example.com:80/x"),
    ("https://example.com/%ED%95%9C%EA%B8%80", "https://example.com/%ED%95%9C%EA%B8%80"),
    ("https://example.com/한글", "https://example.com/한글"),
    ("https://example.com/a%2fb", "https://example.com/a%2fb"),
    ("https://example.com/a%2Fb", "https://example.com/a%2Fb"),
    ("https://example.com/a/index.html", "https://example.com/a/index.html"),
    ("https://example.com/a/amp", "https://example.com/a/amp"),
    ("https://example.com/a?b=2&a=1", "https://example.com/a?b=2&a=1"),
    ("https://example.com/a?a=1&b=2", "https://example.com/a?a=1&b=2"),
    ("https://example.com/a?idxno=1&ref=x&fbclid=y", "https://example.com/a?idxno=1"),
    ("https://Example.com/Path", "https://example.com/Path"),
    ("https://example.com", "https://example.com"),
    ("https://example.com/", "https://example.com"),
    ("https://m.example.com/x", "https://m.example.com/x"),
    ("https://example.com/x?", "https://example.com/x"),
    ("//example.com/x", "//example.com/x"),
    ("", ""),
    (" https://example.com/x ", "https://example.com/x"),
]

LEGACY_PARSE_DATE_GOLDEN = [
    ("2026-09-23", date(2026, 9, 23)),
    ("2026-09-23T23:30:00Z", date(2026, 9, 23)),  # 레거시는 시간대를 무시한다
    ("2026-09-23T15:30:00+00:00", date(2026, 9, 23)),
    ("Wed, 23 Sep 2026 15:30:00 GMT", None),
    ("2026.9.23.", date(2026, 9, 23)),
    ("2026년 9월 23일 오후 11시", date(2026, 9, 23)),
    ("20260923", None),
    ("9/23/2026", None),
    ("23.09.2026", None),
    ("2026-09", None),
    ("2026-02-30", None),
    ("2026-09-23 24:00", date(2026, 9, 23)),
    ("", None),
    (None, None),
    ("Updated 2026 09 23 (v3)", date(2026, 9, 23)),
]


class L0GoldenTests(unittest.TestCase):
    def test_normalize_url_output_is_pinned(self):
        for raw, expected in L0_GOLDEN:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_url(raw), expected)

    def test_normalize_url_is_idempotent(self):
        for raw, _ in L0_GOLDEN:
            with self.subTest(raw=raw):
                once = normalize_url(raw)
                self.assertEqual(normalize_url(once), once)


class LegacyParseDateGoldenTests(unittest.TestCase):
    def test_parse_date_returns_date_or_none_as_before(self):
        for raw, expected in LEGACY_PARSE_DATE_GOLDEN:
            with self.subTest(raw=raw):
                self.assertEqual(parse_date(raw), expected)


if __name__ == "__main__":
    unittest.main()

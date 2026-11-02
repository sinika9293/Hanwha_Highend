"""article_key(L1, ak1, tk1) 규칙 테스트. 순수 함수만 검증한다."""

import inspect
import itertools
import re
import unicodedata
import unittest

import article_keys as ak
from article_keys import (
    key_kind,
    make_article_key,
    normalize_percent_encoding,
    normalize_url_l1,
    short_key,
    title_key,
    url_key,
)
from content_quality import normalize_url
from dates import parse_published
from tests.test_golden_l0 import L0_GOLDEN

HEX64 = re.compile(r"^[0-9a-f]{64}$")
D = parse_published("2026-09-23")
TITLE = "서울 한남동 신규 레지던스 공개 소식"

L1_CASES = [
    ("https://example.com:443/x", "https://example.com/x"),
    ("http://example.com:80/x", "http://example.com/x"),
    ("http://example.com:443/x", "http://example.com:443/x"),
    ("https://example.com:80/x", "https://example.com:80/x"),
    ("https://example.com:8443/x", "https://example.com:8443/x"),
    ("http://[::1]:80/x", "http://[::1]/x"),
    ("http://u:p@host.com:80/x", "http://u:p@host.com/x"),
    ("https://example.com/한글", "https://example.com/%ED%95%9C%EA%B8%80"),
    ("https://example.com/%ed%95%9c", "https://example.com/%ED%95%9C"),
    ("https://example.com/%41bc", "https://example.com/Abc"),
    ("https://example.com/a%7Eb", "https://example.com/a~b"),
    ("https://example.com/a%2fb", "https://example.com/a%2Fb"),
    ("https://example.com/Path", "https://example.com/Path"),  # 경로 대소문자 유지
    ("https://example.com/100%", "https://example.com/100%"),  # 잘못된 % 는 그대로
    ("https://example.com/a?b=2&a=1", "https://example.com/a?b=2&a=1"),  # 쿼리 순서 유지
    ("https://example.com/a?idxno=1&utm_source=x", "https://example.com/a?idxno=1"),  # L0 규칙 유지
    ("https://example.com/a/index.html", "https://example.com/a/index.html"),  # 통합 금지
    ("https://example.com/a/amp", "https://example.com/a/amp"),  # 통합 금지
    ("", ""),
]


class L1Tests(unittest.TestCase):
    def test_l1_table(self):
        for raw, expected in L1_CASES:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_url_l1(raw), expected)

    def test_scheme_is_never_unified(self):
        self.assertNotEqual(normalize_url_l1("http://a.com/x"), normalize_url_l1("https://a.com/x"))
        self.assertNotEqual(url_key("http://a.com/x"), url_key("https://a.com/x"))

    def test_nfc_nfd_path_is_not_normalized(self):
        nfc = "https://a.com/" + unicodedata.normalize("NFC", "한글")
        nfd = "https://a.com/" + unicodedata.normalize("NFD", "한글")
        self.assertNotEqual(nfc, nfd)
        self.assertNotEqual(normalize_url_l1(nfc), normalize_url_l1(nfd))

    def test_percent_encoding_helper(self):
        self.assertEqual(normalize_percent_encoding("/a%2fb%7e%41"), "/a%2Fb~A")
        self.assertEqual(normalize_percent_encoding("/가"), "/%EA%B0%80")
        self.assertEqual(normalize_percent_encoding("/%zz%4"), "/%zz%4")

    def test_idempotent_and_l0_inclusion_over_corpus(self):
        corpus = [raw for raw, _ in L0_GOLDEN] + [raw for raw, _ in L1_CASES] + [
            "https://WWW.Example.com:443/Path/?utm_source=a&id=1#x",
            "https://example.com/Path?id=1",
            "HTTPS://example.com:443/Path/?id=1&ref=zz",
        ]
        for raw in corpus:
            with self.subTest(raw=raw):
                once = normalize_url_l1(raw)
                self.assertEqual(normalize_url_l1(once), once)
        for a, b in itertools.combinations(corpus, 2):
            if normalize_url(a) == normalize_url(b):  # L0 가 같으면 L1 도 반드시 같아야 한다
                self.assertEqual(normalize_url_l1(a), normalize_url_l1(b), (a, b))

    def test_l1_merges_port_and_encoding_variants_that_l0_keeps_apart(self):
        a, b = "https://example.com:443/%ed%95%9c", "https://example.com/한"
        self.assertNotEqual(normalize_url(a), normalize_url(b))
        self.assertEqual(normalize_url_l1(a), normalize_url_l1(b))
        self.assertEqual(url_key(a), url_key(b))


class UrlKeyTests(unittest.TestCase):
    def test_format_and_determinism(self):
        key = url_key("https://example.com/a?idxno=1")
        self.assertTrue(key.startswith("ak1:"))
        self.assertRegex(key[4:], HEX64)
        self.assertEqual(len(key), 4 + 64)
        self.assertEqual(key, url_key("https://www.example.com/a/?idxno=1&utm_medium=z#top"))
        self.assertNotEqual(key, url_key("https://example.com/a?idxno=2"))

    def test_empty_url_has_no_url_key(self):
        self.assertIsNone(url_key(""))
        self.assertIsNone(url_key("   "))

    def test_url_wins_over_title_in_make_article_key(self):
        key = make_article_key("https://a.com/x", TITLE, D, "매체")
        self.assertEqual(key_kind(key), "url")


class TitleKeyTests(unittest.TestCase):
    def test_requires_usable_date_and_min_length(self):
        self.assertIsNone(title_key(TITLE, None, "매체"))
        self.assertIsNone(title_key(TITLE, parse_published("Updated 2026 09 23 (v3)"), "매체"))  # low
        self.assertIsNone(title_key("짧은 제목", D, "매체"))  # 정규화 후 8자 미만
        self.assertIsNotNone(title_key(TITLE, parse_published("2026-09-23 14:00"), "매체"))  # medium

    def test_format_and_determinism(self):
        key = title_key(TITLE, D, "한국경제")
        self.assertTrue(key.startswith("tk1:"))
        self.assertRegex(key[4:], HEX64)
        self.assertEqual(key, title_key(TITLE, D, "한국경제"))
        self.assertEqual(key_kind(key), "title")

    def test_collision_avoidance_fields(self):
        base = title_key(TITLE, D, "한국경제")
        self.assertNotEqual(base, title_key(TITLE, parse_published("2026-09-24"), "한국경제"))  # 발행일
        self.assertNotEqual(base, title_key(TITLE, D, "매일경제"))  # 출처
        self.assertNotEqual(base, title_key(TITLE + " 속보", D, "한국경제"))  # 제목

    def test_normalization_ignores_case_punctuation_and_unicode_form(self):
        a = title_key("Seoul Hannam Residence — OPENS!", D, "The Times")
        b = title_key("seoul hannam residence opens", D, "the  times")
        self.assertEqual(a, b)
        nfc = unicodedata.normalize("NFC", TITLE)
        nfd = unicodedata.normalize("NFD", TITLE)
        self.assertNotEqual(nfc, nfd)
        self.assertEqual(title_key(nfc, D, "매체"), title_key(nfd, D, "매체"))

    def test_url_and_title_keys_never_collide(self):
        self.assertNotEqual(ak._sha256_hex("muwa/ak1", "x"), ak._sha256_hex("muwa/tk1", "x"))
        u = make_article_key("https://a.com/x", TITLE, D, "매체")
        t = make_article_key("", TITLE, D, "매체")
        self.assertNotEqual(u, t)
        self.assertEqual((key_kind(u), key_kind(t)), ("url", "title"))

    def test_no_key_without_url_and_conditions(self):
        self.assertIsNone(make_article_key("", "짧음", D, "매체"))
        self.assertIsNone(make_article_key("", TITLE, None, "매체"))


class MiscTests(unittest.TestCase):
    def test_short_key_and_kind(self):
        key = url_key("https://a.com/x")
        s = short_key(key)
        self.assertTrue(s.startswith("ak1:"))
        self.assertEqual(len(s), 4 + 12)
        self.assertEqual(short_key(None), "")
        self.assertIsNone(key_kind(None))
        self.assertIsNone(key_kind("zz:abc"))

    def test_module_is_pure(self):
        src = inspect.getsource(ak)
        for token in ("open(", "write_text", "read_text", "subprocess", "import os", "requests", "urlopen"):
            self.assertNotIn(token, src)


if __name__ == "__main__":
    unittest.main()

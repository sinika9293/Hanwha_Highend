import ast
import hashlib
import json
import os
import subprocess
import sys
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

import public_validation as pv
from public_validation import FieldSpec, ValidationError

ROOT = Path(__file__).resolve().parent.parent


class NoNetworkCase(unittest.TestCase):
    def setUp(self):
        self.net_calls = []

        def trip(*args, **kwargs):
            self.net_calls.append(args)
            raise AssertionError("network call attempted")

        for target in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo"):
            patcher = mock.patch(target, trip)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.assertEqual(self.net_calls, [], "network must not be touched")

    def reject(self, func, *args, code=None, **kwargs):
        with self.assertRaises(ValidationError) as cm:
            func(*args, **kwargs)
        if code is not None:
            self.assertEqual(cm.exception.code, code)
        return cm.exception


class IdEnumRefDateHashTests(NoNetworkCase):
    def test_id(self):
        for ok in ("a", "demo", "oecd_hpi", "a1_b2", "x" * 40, "0123"):
            self.assertEqual(pv.validate_id(ok), ok)
        for bad in ("", "Demo", "has-hyphen", "has space", "x" * 41, "한글", "a.b", "a/b", "a\n", " a", "ａｂ", None, 5, b"a", ["a"]):
            with self.subTest(bad=repr(bad)[:20]):
                self.reject(pv.validate_id, bad)

    def test_enum(self):
        allowed = ("disabled", "approved")
        self.assertEqual(pv.validate_enum("approved", allowed), "approved")
        for bad in ("Approved", "approved ", "", "rejected", None, 1):
            with self.subTest(bad=repr(bad)):
                self.reject(pv.validate_enum, bad, allowed)
        for bad_allowed in ((), [], None, "approved", ("a", 1), ("",)):
            with self.subTest(bad_allowed=repr(bad_allowed)):
                self.reject(pv.validate_enum, "a", bad_allowed, code="bad_allowed")

    def test_ref(self):
        for ok in ("EV-0001", "#123", "PR-45", "owner-handle", "a.b_c:d", "x" * 64):
            self.assertEqual(pv.validate_ref(ok), ok)
        for bad in ("", " EV-1", "EV 1", "-start", "x" * 65, "한글", "a/b", "a\tb", "sk-ant-abcdefgh12345678",
                    "0123456789abcdef0123456789abcdef", "AbCdEfGhIjKlMnOpQrStUvWx1234", None, 3):
            with self.subTest(bad=repr(bad)[:30]):
                self.reject(pv.validate_ref, bad)

    def test_date(self):
        for ok in ("2026-11-02", "2024-02-29", "2000-01-01", "2199-12-31"):
            self.assertEqual(pv.validate_date(ok), ok)
        for bad in ("2026-9-1", "20261102", "2026-11-02T00:00:00", "2026-11-31", "2025-02-29", "2026-13-01", "2026-00-10",
                    "1999-12-31", "2200-01-01", "２０２６-１１-０２", "٢٠٢٦-٠٩-٠١", " 2026-11-02", "2026-11-02 ", "", None, 20261102):
            with self.subTest(bad=repr(bad)):
                self.reject(pv.validate_date, bad)

    def test_hash(self):
        ok = "a" * 64
        self.assertEqual(pv.validate_hash(ok), ok)
        for bad in ("A" * 64, "a" * 63, "a" * 65, "g" * 64, "", None, "a" * 32 + " " + "a" * 31):
            with self.subTest(bad=repr(bad)[:20]):
                self.reject(pv.validate_hash, bad)


class HostnameTests(NoNetworkCase):
    def test_valid(self):
        for ok in ("example.org", "api.example.org", "a1.b2.example.co.kr", "xn--e1afmkfd.xn--p1ai", "my-site.example.com",
                   "a" * 63 + ".org", "x.y.z.example.net"):
            with self.subTest(ok=ok):
                self.assertEqual(pv.validate_hostname(ok), ok)

    def test_ip_literals_and_numeric_forms_rejected(self):
        for bad in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "192.168.1.1", "255.255.255.255", "127.1", "0x7f.0x1", "0177.0.0.1",
                    "2130706433", "::1", "[::1]", "2001:db8::1", "1.2.3.4.5", "0.0.0.0", "08.08.08.08"):
            with self.subTest(bad=bad):
                self.reject(pv.validate_hostname, bad)

    def test_internal_and_reserved(self):
        for bad in ("localhost", "foo.localhost", "printer.local", "db.internal", "a.intranet", "x.lan", "x.home", "x.corp",
                    "x.private", "x.test", "x.invalid", "x.example", "x.onion", "1.0.0.127.in-addr.arpa", "host.localdomain"):
            with self.subTest(bad=bad):
                self.reject(pv.validate_hostname, bad)

    def test_syntax(self):
        for bad in ("Example.org", "EXAMPLE.ORG", "example.org.", "*.example.org", "example.org:443", "example.org:80", "example",
                    "a..b.org", ".example.org", "-a.example.org", "a-.example.org", "a_b.example.org", "a b.example.org",
                    "a" * 64 + ".org", ("a" * 60 + ".") * 5 + "org", "", "example.o", "example.123", "example.c0m"):
            with self.subTest(bad=bad[:30]):
                self.reject(pv.validate_hostname, bad)

    def test_unicode_and_hidden_characters(self):
        for bad in ("한글.kr", "bücher.example.org", "ｅｘａｍｐｌｅ.org", "exa\u200bmple.org", "exa\u202emple.org", "example\u2060.org",
                    "example\ufeff.org", "exa\u0000mple.org", "exa\u00admple.org", "example.org\n", "ex ample.org", "例え.jp"):
            with self.subTest(bad=ascii(bad)):
                self.reject(pv.validate_hostname, bad)

    def test_punycode_must_decode(self):
        self.assertEqual(pv.validate_hostname("xn--bcher-kva.example.org"), "xn--bcher-kva.example.org")
        for bad in ("xn--.example.org", "xn---.example.org", "xn--zz--.example.org", "xn--a.example.org.", "xn--é.example.org"):
            with self.subTest(bad=ascii(bad)):
                self.reject(pv.validate_hostname, bad)

    def test_kelvin_sign_normalizes_to_ascii_uppercase_and_is_rejected(self):
        # U+212A(KELVIN SIGN)은 NFC에서 'K'가 된다 -> 대문자 규칙으로 거부
        self.reject(pv.validate_hostname, "\u212aexample.org")


class UrlTests(NoNetworkCase):
    def test_valid(self):
        for ok in ("https://example.org", "https://example.org/", "https://example.org/terms", "https://example.org:443/x",
                   "https://api.example.org/v1/licenses/by/4.0/", "https://creativecommons.org/licenses/by/4.0/legalcode",
                   "https://example.org/a%20b/c", "https://example.org/terms-and-conditions-of-service-for-api-users",
                   "https://example.org/a_b/c~d/e+f/g:h", "https://xn--e1afmkfd.xn--p1ai/x", "https://example.org/" + "x" * 250):
            with self.subTest(ok=ok[:50]):
                self.assertEqual(pv.validate_url(ok), ok)

    def test_scheme_and_structure(self):
        for bad, code in (("http://example.org/x", "scheme"), ("HTTPS://example.org/x", "scheme"), ("ftp://example.org/x", "scheme"),
                          ("file:///etc/passwd", "scheme"), ("//example.org/x", "scheme"), ("example.org/x", "scheme"),
                          ("https:example.org", "scheme"), ("https:///x", "host"), ("javascript:alert(1)", "scheme")):
            with self.subTest(bad=bad):
                self.reject(pv.validate_url, bad, code=code)

    def test_userinfo_query_fragment(self):
        self.reject(pv.validate_url, "https://user@example.org/x", code="userinfo")
        self.reject(pv.validate_url, "https://user:pw@example.org/x", code="userinfo")
        self.reject(pv.validate_url, "https://example.org@evil.org/x", code="userinfo")
        self.reject(pv.validate_url, "https://example.org/x?a=1", code="query")
        self.reject(pv.validate_url, "https://example.org/x?", code="query")
        self.reject(pv.validate_url, "https://example.org?token=abc", code="query")
        self.reject(pv.validate_url, "https://example.org/x#frag", code="fragment")
        self.reject(pv.validate_url, "https://example.org/x#", code="fragment")

    def test_hosts_ip_and_ports(self):
        for bad in ("https://127.0.0.1/x", "https://10.0.0.5/x", "https://169.254.169.254/latest/meta-data/", "https://127.1/x",
                    "https://[::1]/x", "https://[2001:db8::1]:443/x", "https://localhost/x", "https://printer.local/x",
                    "https://2130706433/x", "https://0x7f.0x1/x", "https://EXAMPLE.org/x", "https://example.org./x",
                    "https://exa mple.org/x", "https://*.example.org/x"):
            with self.subTest(bad=bad):
                self.reject(pv.validate_url, bad)
        for bad in ("https://example.org:8443/x", "https://example.org:80/x", "https://example.org:/x", "https://example.org:0443/x",
                    "https://example.org:443:443/x", "https://example.org:abc/x"):
            with self.subTest(bad=bad):
                self.reject(pv.validate_url, bad)

    def test_path_characters_and_encodings(self):
        for bad, code in (("https://example.org/a\\b", "backslash"), ("https://example.org/a b", None), ("https://example.org/a\tb", None),
                          ("https://example.org/a\nb", None), ("https://example.org/\u00e9", None), ("https://example.org/한글", None),
                          ("https://example.org/a%00b", "control_escape"), ("https://example.org/a%0Ab", "control_escape"),
                          ("https://example.org/a%7Fb", "control_escape"), ("https://example.org/%2e%2e/x", "dot_segment"),
                          ("https://example.org/%2E/x", "dot_segment"), ("https://example.org/a/../b", "dot_segment"),
                          ("https://example.org/./b", "dot_segment"), ("https://example.org/a%2Fb", "encoded_separator"),
                          ("https://example.org/a%5Cb", "encoded_separator"), ("https://example.org/%252e%252e", "double_encoding"),
                          ("https://example.org/a%zz", "bad_percent"), ("https://example.org/a%", "bad_percent"),
                          ("https://example.org/a%2", "bad_percent"), ("https://example.org/a'b", "path_chars"),
                          ("https://example.org/a,b", "path_chars"), ("https://example.org/a;b", "path_chars"),
                          ("https://example.org/a@b", "path_chars"), ("https://example.org/a|b", "path_chars"),
                          ("https://example.org/a<b>", "path_chars"), ("https://example.org/a=b", "path_chars")):
            with self.subTest(bad=ascii(bad)):
                self.reject(pv.validate_url, bad, code=code)

    def test_length_limit(self):
        base = "https://example.org/"
        self.assertEqual(len(pv.validate_url(base + "x" * (300 - len(base)))), 300)
        self.reject(pv.validate_url, base + "x" * (301 - len(base)), code="too_long")
        self.reject(pv.validate_url, "https://example.org/" + "a" * 5000, code="too_long")

    def test_secret_like_paths(self):
        for bad in ("https://example.org/key/0123456789abcdef0123456789abcdef01234567", "https://example.org/AbCdEfGhIjKlMnOpQrStUvWx1234",
                    "https://example.org/sk-ant-abcdefghijklmnop", "https://example.org/x/aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3",
                    "https://example.org/v1/ghp-0123456789abcdefghij0123456789abcdef", "https://example.org/AbcDefGhiJklMnoPqrStuVwxYzAbcDef"):
            with self.subTest(bad=bad):
                self.reject(pv.validate_url, bad, code="secret_like")

    def test_hidden_and_bidi_characters(self):
        for ch in ("\u200b", "\u200d", "\u2060", "\ufeff", "\u202e", "\u2066", "\u200f", "\u061c", "\u00a0", "\u3000", "\u2028", "\x00", "\x7f"):
            with self.subTest(ch=hex(ord(ch))):
                self.reject(pv.validate_url, "https://example.org/a" + ch + "b")
                self.reject(pv.validate_url, "https://exam" + ch + "ple.org/ab")

    def test_non_string(self):
        for bad in (None, 5, b"https://example.org", ["https://example.org"]):
            self.reject(pv.validate_url, bad, code="not_a_string")


class TextTests(NoNetworkCase):
    def test_accepts_ordinary_text(self):
        for ok in ("한화 하이엔드 리빙 인텔리전스", "Source: Example Org (CC BY 4.0)", "a", "x" * 200, "공공누리 제1유형 · 출처표시"):
            with self.subTest(ok=ok[:20]):
                self.assertEqual(pv.validate_text(ok), ok)

    def test_returns_nfc(self):
        decomposed = unicodedata.normalize("NFD", "한글 텍스트")
        self.assertNotEqual(decomposed, "한글 텍스트")
        self.assertEqual(pv.validate_text(decomposed), unicodedata.normalize("NFC", "한글 텍스트"))
        self.assertEqual(pv.validate_text("e\u0301"), "\u00e9")

    def test_rejects_control_bidi_zero_width_and_odd_spaces(self):
        for ch in ("\n", "\r", "\t", "\x00", "\x1b", "\x7f", "\x85", "\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u202a", "\u202e",
                   "\u2066", "\u2069", "\u200e", "\u200f", "\u061c", "\u00ad", "\u2028", "\u2029", "\u00a0", "\u3000", "\u2003",
                   "\ue000", "\ud800"):
            with self.subTest(ch=hex(ord(ch))):
                self.reject(pv.validate_text, "ab" + ch + "cd")

    def test_edge_spaces_empty_and_length(self):
        self.reject(pv.validate_text, " lead", code="edge_space")
        self.reject(pv.validate_text, "trail ", code="edge_space")
        self.reject(pv.validate_text, "", code="empty")
        self.assertEqual(pv.validate_text("", allow_empty=True), "")
        self.reject(pv.validate_text, "x" * 201, code="too_long")
        self.assertEqual(pv.validate_text("x" * 50, max_length=50), "x" * 50)
        self.reject(pv.validate_text, "x" * 51, max_length=50, code="too_long")
        self.reject(pv.validate_text, None, code="not_a_string")

    def test_existing_public_safety_rules_apply_per_value(self):
        for bad in ("see https://example.org/x", "HTTP://example.org", "key sk-ant-abcdefgh12345678", "path C:\\Users\\someone\\f.py",
                    "/home/someone/x", "/Users/someone", "ak1:abcdef", "article_key", "token=abcd1234efgh", "ghp_abcdefghijklmnopqrstuvwx",
                    "Authorization: Bearer abcdefghijklmnop"):
            with self.subTest(bad=bad[:30]):
                self.reject(pv.validate_text, bad, code="unsafe_content")

    def test_http_substring_without_scheme_is_fine(self):
        self.assertEqual(pv.validate_text("httpxError and HTTPStatus"), "httpxError and HTTPStatus")


class DispatchAndRecordTests(NoNetworkCase):
    SAMPLES = {
        "id": "demo_source", "ref": "EV-0001", "date": "2026-11-02", "hostname": "api.example.org", "hash": "b" * 64,
        "url": "https://example.org/terms", "text": "Example Org attribution",
    }

    def test_unknown_kind_and_options_rejected(self):
        self.reject(pv.validate_field, "uuid", "x", code="unknown_kind")
        self.reject(pv.validate_field, "", "x", code="unknown_kind")
        self.reject(pv.validate_field, None, "x", code="unknown_kind")
        self.reject(pv.validate_field, "id", "demo", bad_option=True, code="bad_option")
        self.reject(pv.validate_field, "date", "2026-11-02", max_length=5, code="bad_option")
        self.assertEqual(pv.validate_field("id", "demo", max_length=10), "demo")

    def test_every_kind_is_dispatched_and_listed(self):
        self.assertEqual(set(pv.KINDS), set(pv._VALIDATORS))
        for kind, sample in self.SAMPLES.items():
            self.assertEqual(pv.validate_field(kind, sample), sample)
        self.assertEqual(pv.validate_field("enum", "a", allowed=("a", "b")), "a")

    def test_url_is_not_accepted_as_any_other_kind(self):
        url = self.SAMPLES["url"]
        for kind in ("id", "ref", "date", "hostname", "hash", "text"):
            with self.subTest(kind=kind):
                self.reject(pv.validate_field, kind, url)
        self.reject(pv.validate_field, "enum", url, allowed=("a", "b"))

    def test_hostname_is_not_accepted_as_url_or_hash_or_date_or_id(self):
        host = self.SAMPLES["hostname"]
        for kind in ("url", "hash", "date", "id"):
            with self.subTest(kind=kind):
                self.reject(pv.validate_field, kind, host)

    def test_other_cross_kind_mismatches(self):
        for kind, wrong in (("date", self.SAMPLES["hash"]), ("hash", self.SAMPLES["date"]), ("hostname", self.SAMPLES["id"]),
                            ("id", self.SAMPLES["text"]), ("url", self.SAMPLES["text"]), ("hostname", self.SAMPLES["url"])):
            with self.subTest(kind=kind):
                self.reject(pv.validate_field, kind, wrong)

    def schema(self):
        return {
            "source_id": FieldSpec("id"), "evidence_id": FieldSpec("ref"), "checked_on": FieldSpec("date"),
            "host": FieldSpec("hostname"), "terms_sha256": FieldSpec("hash"), "terms_url": FieldSpec("url"),
            "attribution": FieldSpec("text", options={"max_length": 100}),
            "status": FieldSpec("enum", options={"allowed": ("disabled", "approved")}),
            "note": FieldSpec("text", required=False, options={"allow_empty": True}),
        }

    def record(self, **over):
        rec = {"source_id": "demo", "evidence_id": "EV-0001", "checked_on": "2026-11-02", "host": "api.example.org",
               "terms_sha256": "c" * 64, "terms_url": "https://example.org/terms", "attribution": "Example Org (CC BY 4.0)",
               "status": "disabled"}
        rec.update(over)
        return rec

    def test_valid_record_returns_normalized_copy(self):
        rec = self.record(attribution=unicodedata.normalize("NFD", "한글 출처"))
        original = dict(rec)
        out = pv.validate_record(rec, self.schema())
        self.assertEqual(out["attribution"], unicodedata.normalize("NFC", "한글 출처"))
        self.assertEqual(rec, original)  # 입력은 바뀌지 않는다
        self.assertIsNot(out, rec)
        self.assertNotIn("note", out)

    def test_record_rejections(self):
        schema = self.schema()
        self.reject(pv.validate_record, self.record(extra="x"), schema, code="unknown_field")
        self.reject(pv.validate_record, self.record(**{"Bad-Key": "x"}), schema, code="bad_key")
        self.reject(pv.validate_record, {1: "x"}, schema, code="bad_key")
        rec = self.record()
        del rec["host"]
        err = self.reject(pv.validate_record, rec, schema, code="missing_field")
        self.assertEqual(err.field, "host")
        err = self.reject(pv.validate_record, self.record(terms_url="http://example.org/terms"), schema)
        self.assertEqual((err.field, err.kind), ("terms_url", "url"))
        # 한 필드에 다른 유형의 값을 넣으면 거부된다
        err = self.reject(pv.validate_record, self.record(host="https://example.org/terms"), schema)
        self.assertEqual(err.field, "host")
        self.reject(pv.validate_record, self.record(attribution="https://example.org/terms"), schema, code="unsafe_content")
        self.reject(pv.validate_record, self.record(status="approved "), schema, code="not_allowed")
        for non_str in (5, True, None, ["a"], {"a": "b"}, b"abc"):
            with self.subTest(value=repr(non_str)):
                self.reject(pv.validate_record, self.record(source_id=non_str), schema, code="not_a_string")
        self.reject(pv.validate_record, [self.record()], schema, code="not_object")
        self.reject(pv.validate_record, "text", schema, code="not_object")

    def test_bad_schemas_rejected(self):
        for bad in (None, {}, [], {"Bad": FieldSpec("id")}, {"a": "id"}, {"a": FieldSpec("uuid")},
                    {"a": FieldSpec("id", options={"nope": 1})}, {"a": FieldSpec("enum")}):
            with self.subTest(bad=repr(bad)[:40]):
                with self.assertRaises(ValidationError):
                    pv.validate_record({"a": "x"}, bad)

    def test_errors_never_echo_the_input(self):
        secret = "sk-ant-SECRETVALUE123456"
        cases = [
            lambda: pv.validate_text("note " + secret),
            lambda: pv.validate_url("https://example.org/" + secret),
            lambda: pv.validate_ref(secret),
            lambda: pv.validate_id(secret),
            lambda: pv.validate_hostname(secret + ".org"),
            lambda: pv.validate_record(self.record(attribution=secret + " https://x.org"), self.schema()),
            lambda: pv.validate_record(self.record(**{"zz_" + "secretkey": secret}), self.schema()),
        ]
        for case in cases:
            with self.assertRaises(ValidationError) as cm:
                case()
            err = cm.exception
            for text in (str(err), repr(err), repr(err.args)):
                self.assertNotIn("SECRETVALUE", text)
                self.assertNotIn("secretkey", text)
            self.assertIsNone(err.__context__)
            self.assertIsNone(err.__cause__)


INVISIBLE_LETTERS = (0x3164, 0xFFA0, 0x115F, 0x1160, 0x2800, 0x17B4, 0x17B5, 0x180B, 0x180C, 0x180D, 0x180F, 0x034F, 0x1D159,
                     0xFE00, 0xFE01, 0xFE0E, 0xFE0F, 0xE0100, 0xE0101, 0xE01EF)


class InvisibleCharacterTests(NoNetworkCase):
    """리뷰 M4: 범주만으로는 걸러지지 않는 '보이지 않는' 글자."""

    def test_every_listed_character_is_rejected_in_text(self):
        for code_point in INVISIBLE_LETTERS:
            ch = chr(code_point)
            for template in ("ab%scd", "%sabcd", "abcd%s", "한화%s리빙"):
                with self.subTest(code_point=hex(code_point), template=template):
                    self.reject(pv.validate_text, template % ch, code="invisible_char")

    def test_strings_made_only_of_invisible_characters_are_rejected(self):
        for value in ("\u3164", "\u3164\u3164\u3164", "\u2800\u2800", "\u115f\u1160", "\ufe0f", "\u034f", "\u180b\u180c",
                      "\U000e0100", "\uffa0\uffa0", "\u17b4\u17b5", "\U0001d159"):
            with self.subTest(value=ascii(value)):
                self.reject(pv.validate_text, value)
        for value in (" \u3164", "\u3164 ", " \u3164 ", "\u3164\u2800\u115f"):
            with self.subTest(value=ascii(value)):
                self.reject(pv.validate_text, value)

    def test_strings_without_any_visible_character_are_rejected(self):
        for value in ("\u0301", "\u0301\u0301", "\u0300\u0301\u0302", "\u0e31", "\u093f", "\u20dd"):
            with self.subTest(value=ascii(value)):
                self.reject(pv.validate_text, value, code="no_visible_char")

    def test_visible_neighbours_of_the_blocked_characters_are_allowed(self):
        for code_point in (0x2801, 0x28FF, 0x17B6, 0x180A, 0xFE10, 0xFE30, 0x3163, 0x3165, 0xFFA1, 0xFF9F, 0x115E, 0x1161, 0x11A8, 0x0350, 0x034E):
            ch = chr(code_point)
            with self.subTest(code_point=hex(code_point)):
                self.assertEqual(pv.validate_text("ab" + ch + "cd"), "ab" + ch + "cd")

    def test_blocked_ranges_have_exact_boundaries(self):
        for code_point in (0xFE00, 0xFE0F, 0xE0100, 0xE01EF):
            self.reject(pv.validate_text, "x" + chr(code_point), code="invisible_char")
        self.assertEqual(pv.validate_text("x\ufe10"), "x\ufe10")  # 변이 선택자 바로 다음의 일반 구두점
        self.reject(pv.validate_text, "x\U000e01f0")  # 범위 밖은 미할당 문자라 다른 규칙으로 거부된다

    def test_legitimate_scripts_with_combining_marks_and_vowel_signs_are_allowed(self):
        for value in ("हिन\u094dदी", "ภาษาไทย", "العربية", "한화 하이엔드 리빙", "日本語のテキスト", "Ελληνικά", "e\u0301", "Привет", "ខ\u17d2មែរ", "⠁⠃⠉ braille"):
            with self.subTest(value=value):
                self.assertEqual(pv.validate_text(value), unicodedata.normalize("NFC", value))

    def test_emoji_presentation_selector_is_rejected_but_plain_emoji_is_allowed(self):
        self.assertEqual(pv.validate_text("\U0001F600"), "\U0001F600")
        self.reject(pv.validate_text, "\u2764\ufe0f")
        self.reject(pv.validate_text, "\u2764\ufe0e")

    def test_ascii_only_kinds_reject_them_as_non_ascii(self):
        ch = "\u3164"
        for func, value in ((pv.validate_id, "ab" + ch), (pv.validate_ref, "EV-" + ch), (pv.validate_hash, "a" * 63 + ch),
                            (pv.validate_hostname, "ex" + ch + "ample.org"), (pv.validate_url, "https://example.org/a" + ch)):
            with self.subTest(func=func.__name__):
                self.reject(func, value, code="non_ascii")

    def test_record_field_errors_name_the_field_not_the_value(self):
        schema = {"attribution": FieldSpec("text")}
        err = self.reject(pv.validate_record, {"attribution": "Example\u3164Org"}, schema, code="invisible_char")
        self.assertEqual((err.field, err.kind), ("attribution", "text"))
        self.assertNotIn("Example", str(err))

    def test_decoded_punycode_labels_are_checked_for_invisible_characters(self):
        for code_point in (0x3164, 0x115F, 0x2800, 0xFFA0, 0x034F):
            label = "xn--" + ("a" + chr(code_point) + "b").encode("punycode").decode("ascii")
            with self.subTest(code_point=hex(code_point)):
                self.reject(pv.validate_hostname, label + ".example.org")
                self.reject(pv.validate_url, "https://" + label + ".example.org/x")

    def test_punycode_labels_made_only_of_combining_marks_are_rejected(self):
        # 표준 idna 코덱이 정상 디코딩하지만 눈에 보이는 글자가 하나도 없는 레이블
        for mark in ("\u0301", "\u0301\u0302", "\u0e31"):
            label = "xn--" + mark.encode("punycode").decode("ascii")
            with self.subTest(label=label):
                self.assertEqual(label.encode("ascii").decode("idna"), mark)
                self.reject(pv.validate_hostname, label + ".example.org", code="punycode")
                self.reject(pv.validate_url, "https://%s.example.org/x" % label, code="punycode")

    def test_decomposed_punycode_labels_never_decode_in_the_first_place(self):
        # NFC 재확인은 이중 방어: 표준 코덱은 분해형 레이블을 디코딩 단계에서 이미 거부한다.
        label = "xn--" + "e\u0301".encode("punycode").decode("ascii")
        with self.assertRaises(UnicodeError):
            label.encode("ascii").decode("idna")
        self.reject(pv.validate_hostname, label + ".example.org", code="punycode")

    def test_documented_set_matches_the_implementation(self):
        for code_point in INVISIBLE_LETTERS:
            self.assertTrue(pv._is_invisible(code_point), hex(code_point))
        for name in ("U+3164", "U+FFA0", "U+115F", "U+1160", "U+2800", "U+17B4", "U+180B", "U+FE00-FE0F", "U+E0100-E01EF", "U+034F"):
            self.assertIn(name, pv.__doc__)


class PercentEncodingStrictnessTests(NoNetworkCase):
    """리뷰 M5: URL 경로의 퍼센트 인코딩은 디코딩한 결과까지 엄격하게 검증한다."""

    BASE = "https://example.org/"

    def url(self, path):
        return self.BASE + path

    def test_valid_utf8_escapes_are_accepted_in_either_hex_case(self):
        for path in ("a%C3%A9b", "a%c3%a9b", "%ED%95%9C%EA%B8%80", "%ed%95%9c%ea%b8%80/terms", "%F0%9F%98%80", "%C3%A9%20%C3%A9", "x%E2%80%93y",
                     "%C2%B7", "%EF%BC%A1", "%CE%91%CE%B2", "%E3%81%82/%E3%81%84", "a%20b%C3%A9c%2Bd"):
            with self.subTest(path=path):
                self.assertEqual(pv.validate_url(self.url(path)), self.url(path))

    def test_invalid_or_overlong_utf8_is_rejected(self):
        for path in ("a%FFb", "%FF", "%FE", "%C0%AF", "%C0%AE", "%C0%80", "%C1%BF", "%C1%9C", "%E0%80%AF", "%E0%9F%BF", "%F0%80%80%AF", "%F0%8F%BF%BF",
                     "%C3", "%E3%85", "%F0%9F%98", "%80", "%BF", "%ED%A0%80", "%ED%BF%BF", "%F4%90%80%80", "%F5%80%80%80", "%F8%88%80%80%80",
                     "%FC%84%80%80%80%80", "%C3%A9%A9", "%C3a%A9", "%A9%C3", "%C3%C3", "%E3%85%2F%A4", "..%C0%AF", "%2e%C0%AE%C0%AE/", "a%c0%afb"):
            with self.subTest(path=path):
                # 인코딩된 슬래시(%2F)가 먼저 걸리는 입력은 코드를 지정하지 않고 거부만 확인한다
                if "%2F" in path.upper():
                    self.reject(pv.validate_url, self.url(path))
                else:
                    self.reject(pv.validate_url, self.url(path), code="invalid_utf8")

    def test_encoded_control_bidi_zero_width_and_invisible_characters_are_rejected(self):
        for path, label in (("%E2%80%AE", "RLO"), ("%E2%80%AD", "LRO"), ("%E2%80%AA", "LRE"), ("%E2%81%A6", "LRI"), ("%E2%81%A9", "PDI"),
                            ("%E2%80%8E", "LRM"), ("%E2%80%8F", "RLM"), ("%D8%9C", "ALM"), ("%E2%80%8B", "ZWSP"), ("%E2%80%8C", "ZWNJ"),
                            ("%E2%80%8D", "ZWJ"), ("%E2%81%A0", "WJ"), ("%EF%BB%BF", "BOM"), ("%C2%AD", "SHY"), ("%C2%80", "C1 control"),
                            ("%C2%85", "NEL"), ("%C2%9F", "C1 end"), ("%E2%80%A8", "LS"), ("%E2%80%A9", "PS"), ("%C2%A0", "NBSP"), ("%E3%80%80", "ideographic space"),
                            ("%E2%80%83", "em space"), ("%EE%80%80", "private use"), ("%F3%B0%80%80", "plane-15 private use"), ("%E3%85%A4", "Hangul filler"),
                            ("%EF%BE%A0", "halfwidth Hangul filler"), ("%E1%85%9F", "U+115F"), ("%E1%85%A0", "U+1160"), ("%E2%A0%80", "braille blank"),
                            ("%E1%9E%B4", "U+17B4"), ("%E1%A0%8B", "U+180B"), ("%EF%B8%8F", "VS16"), ("%EF%B8%80", "VS1"), ("%F3%A0%84%80", "VS17"),
                            ("%CD%8F", "CGJ"), ("%F3%A0%80%81", "tag begin"), ("%F3%A0%81%81", "tag A"), ("%EF%BF%BE", "noncharacter"),
                            ("%F0%9D%85%99", "musical null notehead")):
            with self.subTest(label=label):
                self.reject(pv.validate_url, self.url("a" + path + "b"), code="encoded_forbidden_char")

    def test_encoded_forms_are_rejected_wherever_they_appear(self):
        for path in ("%E2%80%AE", "x/%E2%80%AE", "x/y%E2%80%AEz/w", "a%C3%A9%E2%80%8B", "%E2%80%8B%C3%A9"):
            with self.subTest(path=path):
                self.reject(pv.validate_url, self.url(path), code="encoded_forbidden_char")

    def test_compatibility_forms_of_separators_are_rejected(self):
        for path, label in (("%EF%BC%8F", "fullwidth /"), ("%EF%BC%8E", "fullwidth ."), ("%EF%BC%BC", "fullwidth backslash"), ("%E2%80%A4", "one dot leader"),
                            ("%E2%80%A5", "two dot leader"), ("%E2%80%A6", "ellipsis"), ("%EF%B9%92", "small full stop"), ("%EF%BC%85", "fullwidth %"),
                            ("%EF%BC%9F", "fullwidth ?"), ("%EF%BC%83", "fullwidth #"), ("%EF%BC%A0", "fullwidth @"), ("%EF%BC%9A", "fullwidth :"),
                            ("%E2%84%80", "account of"), ("%E2%84%85", "care of")):
            with self.subTest(label=label):
                self.reject(pv.validate_url, self.url("a" + path + "b"), code="compat_separator")

    def test_double_and_nested_encoding_is_rejected(self):
        for path in ("%25", "a%25b", "%2525", "%252e%252e", "%252e%252e/", "%252F", "%255C", "%2e%252e", "%25%32%65", "%25%32%35", "%2525%32%65", "x%25"):
            with self.subTest(path=path):
                self.reject(pv.validate_url, self.url(path), code="double_encoding")

    def test_non_percent_unicode_escape_syntaxes_are_rejected(self):
        for path in ("%u202e", "%U202E", "%u0041", "%%32%65", "%e", "%", "%g0", "a%"):
            with self.subTest(path=path):
                self.reject(pv.validate_url, self.url(path), code="bad_percent")

    def test_existing_ascii_escape_rules_keep_their_codes(self):
        for path, code in (("a%00b", "control_escape"), ("a%0Ab", "control_escape"), ("a%0d", "control_escape"), ("a%1f", "control_escape"), ("a%7F", "control_escape"),
                           ("a%2Fb", "encoded_separator"), ("a%2fb", "encoded_separator"), ("a%5Cb", "encoded_separator"), ("%2e%2e/x", "dot_segment"),
                           (".%2E/x", "dot_segment")):
            with self.subTest(path=path):
                self.reject(pv.validate_url, self.url(path), code=code)

    def test_raw_non_ascii_is_still_rejected_before_decoding(self):
        for path in ("\u00e9", "한글", "a\u202eb"):
            self.reject(pv.validate_url, self.url(path), code="non_ascii")

    def test_check_applies_in_every_url_position_that_allows_a_path(self):
        self.reject(pv.validate_url, "https://example.org:443/a%E2%80%AEb", code="encoded_forbidden_char")
        self.reject(pv.validate_url, "https://sub.example.org/x/y/z/%C0%AF", code="invalid_utf8")
        schema = {"terms_url": FieldSpec("url")}
        err = self.reject(pv.validate_record, {"terms_url": self.url("%C0%AF")}, schema, code="invalid_utf8")
        self.assertEqual(err.field, "terms_url")


class EnvironmentIndependenceTests(NoNetworkCase):
    """리뷰 L4: 검증 결과는 환경변수에 의존하지 않는다. 환경 값 검사는 별도 함수로만 한다."""

    CORPUS = (
        ("text", "dummy text", {}), ("text", "uses tokenvalue123 here", {}), ("text", "sk-ant-abcdefgh12345678", {}), ("text", "한화 하이엔드", {}),
        ("url", "https://example.org/dummy-docs", {}), ("url", "https://example.org/tokenvalue123", {}), ("url", "https://example.org/%ED%95%9C", {}),
        ("url", "https://example.org/sk-ant-abcdefghijklmnop", {}), ("ref", "dummy-ref", {}), ("ref", "tokenvalue123", {}), ("ref", "EV-0001", {}),
        ("id", "dummy", {}), ("hostname", "dummy.example.org", {}), ("hostname", "tokenvalue123.example.org", {}), ("date", "2026-11-02", {}),
        ("hash", "a" * 64, {}), ("enum", "dummy", {"allowed": ["dummy", "other"]}), ("enum", "nope", {"allowed": ["dummy"]}),
    )
    ENV = {"ANTHROPIC_API_KEY": "dummy", "GITHUB_TOKEN": "tokenvalue123", "API_KEY": "tokenvalue123", "SECRET": "dummy"}

    @staticmethod
    def outcome(kind, value, options):
        try:
            return ["ok", pv.validate_field(kind, value, **options)]
        except pv.ValidationError as e:
            return ["rej", e.code]

    def outcomes(self):
        return [self.outcome(*case) for case in self.CORPUS]

    def test_in_process_results_do_not_change_with_environment_values(self):
        import backends, ops_state  # noqa: F401  (지연 임포트가 환경을 읽는 일이 없도록 먼저 불러 둔다)

        without = {k: v for k, v in os.environ.items() if k not in self.ENV}
        with mock.patch.dict(os.environ, without, clear=True):
            baseline = self.outcomes()
        with mock.patch.dict(os.environ, self.ENV):
            self.assertEqual(self.outcomes(), baseline)
        self.assertTrue(any(o[0] == "ok" for o in baseline) and any(o[0] == "rej" for o in baseline))

    def test_values_equal_to_environment_secrets_are_accepted_by_field_validators(self):
        with mock.patch.dict(os.environ, self.ENV):
            self.assertEqual(pv.validate_url("https://example.org/dummy-docs"), "https://example.org/dummy-docs")
            self.assertEqual(pv.validate_ref("dummy-ref"), "dummy-ref")
            self.assertEqual(pv.validate_text("dummy text"), "dummy text")
            self.assertEqual(pv.validate_ref("tokenvalue123"), "tokenvalue123")

    def test_validators_never_touch_the_environment(self):
        import backends, ops_state  # noqa: F401

        class Tripwire:
            def _trip(self, *args, **kwargs):
                raise AssertionError("os.environ was read by a validator")

            __getitem__ = get = __contains__ = __iter__ = __len__ = keys = items = values = copy = _trip

        with mock.patch.object(os, "environ", Tripwire()):
            self.outcomes()
            pv.validate_record({"u": "https://example.org/x"}, {"u": FieldSpec("url")})

    def test_secret_pattern_check_is_called_with_an_empty_environment(self):
        import backends

        calls = []
        real = backends.mask_secrets

        def spy(text, environ=None):
            calls.append(environ)
            return real(text, environ)

        with mock.patch("backends.mask_secrets", spy):
            pv.validate_url("https://example.org/terms")
            pv.validate_ref("EV-0001")
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(environ == {} and environ is not None for environ in calls))

    def test_patterns_are_still_detected_without_the_environment(self):
        self.reject(pv.validate_url, "https://example.org/sk-ant-abcdefghijklmnop", code="secret_like")
        self.reject(pv.validate_ref, "ghp-0123456789abcdefghij0123456789abcdef", code="secret_like")
        self.reject(pv.validate_text, "key sk-ant-abcdefgh12345678", code="unsafe_content")

    def test_subprocess_results_match_with_and_without_the_environment(self):
        code = ("import json, sys\nsys.path.insert(0, %r)\nimport public_validation as pv\nout = []\n"
                "for kind, value, options in json.loads(sys.argv[1]):\n    try:\n        out.append(['ok', pv.validate_field(kind, value, **options)])\n"
                "    except pv.ValidationError as e:\n        out.append(['rej', e.code])\nprint(json.dumps(out))\n") % str(ROOT)
        arg = json.dumps(self.CORPUS)
        results = []
        for extra in ({}, self.ENV):
            env = {k: v for k, v in os.environ.items() if k not in self.ENV}
            env.update(extra)
            env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
            r = subprocess.run([sys.executable, "-B", "-c", code, arg], capture_output=True, text=True, encoding="utf-8", errors="replace",
                               env=env, cwd=str(ROOT), timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            results.append(json.loads(r.stdout))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], self.outcomes())

    def test_contains_env_secret_is_a_separate_explicit_check(self):
        env = {"ANTHROPIC_API_KEY": "dummy-secret-value"}
        self.assertTrue(pv.contains_env_secret("note: dummy-secret-value inside", env))
        self.assertFalse(pv.contains_env_secret("note: nothing here", env))
        self.assertFalse(pv.contains_env_secret("note: dummy-secret-value inside", {}))
        self.assertFalse(pv.contains_env_secret("", env))
        with self.assertRaises(TypeError):
            pv.contains_env_secret("x")  # environ 을 생략할 수 없다 (암묵적 os.environ 사용 금지)
        with self.assertRaises(TypeError):
            pv.contains_env_secret("x", "not a mapping")
        self.reject(pv.contains_env_secret, 5, env, code="not_a_string")

    def test_an_empty_mapping_is_not_replaced_by_os_environ(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "dummy-secret-value"}):
            self.assertFalse(pv.contains_env_secret("note: dummy-secret-value inside", {}))
            self.assertTrue(pv.contains_env_secret("note: dummy-secret-value inside", os.environ))
            self.assertTrue(pv.contains_env_secret("note: dummy-secret-value inside", {"ANTHROPIC_API_KEY": "dummy-secret-value"}))

    def test_field_validators_never_call_the_env_secret_check(self):
        with mock.patch.object(pv, "contains_env_secret", side_effect=AssertionError("validators must not use the env check")):
            self.outcomes()


class ReviewGapTests(NoNetworkCase):
    """리뷰 T1~T3, T7: 기존 테스트가 다른 규칙에 가려져 잡지 못했던 경계."""

    def test_t1_single_label_hosts_that_are_not_reserved(self):
        for host in ("printer", "db01", "intranet-server", "nas", "router", "metadata", "kubernetes", "vault", "a", "x" * 63):
            with self.subTest(host=host):
                self.reject(pv.validate_hostname, host, code="single_label")
                err = self.reject(pv.validate_url, "https://%s/x" % host, code="single_label")
                self.assertEqual(err.kind, "url")
        self.assertEqual(pv.validate_hostname("printer.example.org"), "printer.example.org")

    def test_t2_invalid_punycode_that_passes_ldh_syntax(self):
        for host in ("xn--a.example.org", "xn--bcher-kvb.example.org", "xn--9999999999.example.org", "xn--0.example.org", "xn--zz.example.org",
                     "xn--u-ccb.example.org", "www.xn--a.example.org", "example.xn--a", "xn--a.xn--b"):
            with self.subTest(host=host):
                self.reject(pv.validate_hostname, host, code="punycode")
                self.reject(pv.validate_url, "https://%s/x" % host, code="punycode")

    def test_t2_valid_punycode_is_accepted(self):
        for host in ("xn--bcher-kva.example.org", "xn--e1afmkfd.xn--p1ai", "a.xn--e1afmkfd.xn--p1ai", "xn--80ak6aa92e.com"):
            with self.subTest(host=host):
                self.assertEqual(pv.validate_hostname(host), host)
                self.assertEqual(pv.validate_url("https://%s/x" % host), "https://%s/x" % host)

    def test_t2_decomposed_punycode_labels_are_rejected(self):
        label = "xn--" + "e\u0301".encode("punycode").decode("ascii")
        self.reject(pv.validate_hostname, label + ".example.org")

    def test_t3_all_letter_hex_runs_of_32_or_more_are_secret_like(self):
        for token in ("deadbeef" * 4, "abcdef" * 7, "a" * 32, "DEADBEEF" * 4, "fedcba" * 6, "ABCDEF" * 6):
            with self.subTest(token=token):
                self.reject(pv.validate_url, "https://example.org/" + token, code="secret_like")
                self.reject(pv.validate_url, "https://example.org/x/%s/y" % token, code="secret_like")
                self.reject(pv.validate_ref, token, code="secret_like")

    def test_t3_hex_run_boundary_is_32_characters(self):
        for token in ("abcdef" * 5 + "a", "a" * 31, "dead" * 7 + "bee"):
            self.assertEqual(len(token), 31)
            with self.subTest(token=token):
                self.assertEqual(pv.validate_url("https://example.org/" + token), "https://example.org/" + token)
                self.assertEqual(pv.validate_ref(token), token)
        self.reject(pv.validate_ref, "a" * 32, code="secret_like")

    def test_t3_separated_chunks_and_uuids_are_not_treated_as_one_token(self):
        for value in ("deadbeef-deadbeef-deadbeef-deadbeef", "123e4567-e89b-12d3-a456-426614174000", "terms-and-conditions-of-service-for-api-users"):
            with self.subTest(value=value):
                self.assertEqual(pv.validate_url("https://example.org/" + value), "https://example.org/" + value)

    def test_t7_enum_is_normalized_to_nfc_before_matching(self):
        nfd = unicodedata.normalize("NFD", "한글")
        self.assertNotEqual(nfd, "한글")
        self.assertEqual(pv.validate_enum(nfd, ("한글", "영문")), "한글")
        self.assertEqual(pv.validate_enum("e\u0301", ("\u00e9",)), "\u00e9")
        self.assertEqual(pv.validate_enum("\u00e9", ("\u00e9",)), "\u00e9")
        out = pv.validate_record({"lang": nfd}, {"lang": FieldSpec("enum", options={"allowed": ("한글",)})})
        self.assertEqual(out["lang"], "한글")

    def test_t7_enum_stays_exact_apart_from_nfc(self):
        nfd = unicodedata.normalize("NFD", "한글")
        self.reject(pv.validate_enum, nfd, (nfd,), code="not_allowed")  # 허용 목록이 NFC가 아니면 일치하지 않는다 (fail-closed)
        for bad in ("한글 ", " 한글", "한글\n", "HANGUL", "ｈａｎ", "한 글"):
            with self.subTest(bad=ascii(bad)):
                self.reject(pv.validate_enum, bad, ("한글", "hangul"), code="not_allowed")
        self.reject(pv.validate_enum, "x" * 257, ("x",), code="too_long")
        self.reject(pv.validate_enum, 5, ("x",), code="not_a_string")


class LegitimateInputRegressionTests(NoNetworkCase):
    """정상 한글 텍스트·URL·SHA-256 해시는 허용된다 (새 규칙이 정상 입력을 막지 않는다)."""

    def test_korean_and_mixed_text(self):
        for value in ("한화 하이엔드 리빙 인텔리전스", "(주)한화 · 2026년 11월 보고서", "공공누리 제1유형 · 출처표시", "Source: Example Org (CC BY 4.0)",
                      "Café résumé naïve", "価格は¥1,000です", "중앙일보 / 한국경제 – 주간 동향", "A–B, C—D, “인용”, ‘따옴표’", "100% 확인 · 50% 이상", "①②③ 단계"):
            with self.subTest(value=value):
                self.assertEqual(pv.validate_text(value), unicodedata.normalize("NFC", value))

    def test_korean_text_is_returned_in_nfc(self):
        value = unicodedata.normalize("NFD", "한글 텍스트 · 하이엔드")
        self.assertEqual(pv.validate_text(value), unicodedata.normalize("NFC", value))

    def test_ordinary_and_korean_percent_encoded_urls(self):
        for url in ("https://example.org/terms", "https://www.law.go.kr/lsInfoP.do", "https://kogl.or.kr/info/licenseType1.do",
                    "https://creativecommons.org/licenses/by/4.0/legalcode.ko", "https://example.org/%ED%95%9C%EA%B8%80/%EC%95%BD%EA%B4%80",
                    "https://example.org/a%20b/c-d_e~f", "https://example.org:443/x", "https://api.example.co.kr/v1/policies/terms-of-use",
                    "https://example.org/" + "path/" * 20):
            with self.subTest(url=url):
                self.assertEqual(pv.validate_url(url), url)

    def test_sha256_hashes(self):
        for digest in (hashlib.sha256(b"abc").hexdigest(), hashlib.sha256(b"").hexdigest(), hashlib.sha256("한글".encode("utf-8")).hexdigest(), "0" * 64, "f" * 64):
            with self.subTest(digest=digest[:8]):
                self.assertEqual(pv.validate_hash(digest), digest)
        self.reject(pv.validate_hash, hashlib.sha256(b"abc").hexdigest().upper())
        self.reject(pv.validate_hash, hashlib.sha256(b"abc").hexdigest()[:-1])

    def test_a_full_registry_like_record(self):
        schema = {"source_id": FieldSpec("id"), "name": FieldSpec("text"), "attribution": FieldSpec("text"), "terms_url": FieldSpec("url"),
                  "terms_sha256": FieldSpec("hash"), "checked_on": FieldSpec("date"), "host": FieldSpec("hostname"), "status": FieldSpec("enum", options={"allowed": ("disabled", "approved")}),
                  "evidence_id": FieldSpec("ref")}
        record = {"source_id": "kogl_demo", "name": "공공누리 데모 출처", "attribution": "출처: 한국저작권위원회 (공공누리 제1유형)",
                  "terms_url": "https://www.kogl.or.kr/info/licenseType1.do", "terms_sha256": hashlib.sha256(b"terms").hexdigest(),
                  "checked_on": "2026-11-02", "host": "www.kogl.or.kr", "status": "disabled", "evidence_id": "EV-0001"}
        self.assertEqual(pv.validate_record(record, schema), record)


class DependencyAndImportTests(unittest.TestCase):
    def run_py(self, code):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, "-B", "-c", "import sys; sys.path.insert(0, %r)\n%s" % (str(ROOT), code)],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, cwd=str(ROOT), timeout=120)

    def test_import_does_not_load_ops_state_or_backends(self):
        r = self.run_py("import public_validation\nprint('ops_state' in sys.modules, 'backends' in sys.modules, 'anthropic' in sys.modules)")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "False False False")

    def test_no_circular_import_in_either_order(self):
        for code in ("import public_validation, ops_state", "import ops_state, public_validation",
                     "import ops_state\nimport public_validation as p\np.validate_text('ok')\np.validate_url('https://example.org/x')"):
            with self.subTest(code=code[:40]):
                r = self.run_py(code + "\nprint('OK')")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("OK", r.stdout)

    def test_simulated_future_cycle_is_safe(self):
        # ops_state 가 나중에 public_validation 을 import 하더라도, 지연 임포트라 순환이 생기지 않는다.
        code = ("import ops_state\nimport types\nops_state.public_validation = __import__('public_validation')\n"
                "ops_state.public_validation.validate_text('ok')\nprint('OK')")
        r = self.run_py(code)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_existing_assert_public_safe_is_untouched(self):
        r = self.run_py("import ops_state\n"
                        "for t in ('https://example.org/x', 'plain'):\n"
                        "    try:\n        ops_state.assert_public_safe(t); print('ok')\n"
                        "    except ops_state.StatusWriteError: print('rejected')")
        self.assertEqual(r.stdout.split(), ["rejected", "ok"])

    def test_standard_library_and_lazy_project_imports_only(self):
        tree = ast.parse((ROOT / "public_validation.py").read_text(encoding="utf-8"))
        top_level, lazy = set(), set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                top_level.add((node.module or "").split(".")[0])
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.col_offset > 0:
                lazy.add((node.module or "").split(".")[0])
        self.assertEqual(top_level - {"__future__", "re", "unicodedata", "dataclasses", "datetime"}, set())
        self.assertEqual(lazy, {"backends", "ops_state"})


if __name__ == "__main__":
    unittest.main()

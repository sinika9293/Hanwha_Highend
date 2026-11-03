import ast
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import strict_json
from strict_json import MAX_BYTES, MAX_DEPTH, StrictJsonError

ROOT = Path(__file__).resolve().parent.parent
SECRET = "sk-ant-SECRETVALUE123456"


class NoNetworkCase(unittest.TestCase):
    """이 모듈은 네트워크를 쓰지 않는다: 소켓 호출이 한 번이라도 일어나면 실패한다."""

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


def code_of(func, *args, **kwargs):
    try:
        func(*args, **kwargs)
    except StrictJsonError as e:
        return e.code
    raise AssertionError("expected StrictJsonError")


class AcceptTests(NoNetworkCase):
    def test_valid_documents(self):
        for text, expected in (
            ('{}', {}),
            ('  {"a": 1, "b": [true, null, 1.5, "x"], "c": {"d": {}}}\n', {"a": 1, "b": [True, None, 1.5, "x"], "c": {"d": {}}}),
            ('{"한글": "값", "emoji": "\\ud83d\\ude00"}', {"한글": "값", "emoji": "\U0001F600"}),
            ('{"A": 1, "a": 2}', {"A": 1, "a": 2}),  # 대소문자가 다르면 다른 키
            ('{"n": -0, "e": 1e3, "big": 123456789012345678901234567890}', {"n": 0, "e": 1000.0, "big": 123456789012345678901234567890}),
        ):
            with self.subTest(text=text[:30]):
                self.assertEqual(strict_json.loads_text(text), expected)
                self.assertEqual(strict_json.loads_bytes(text.encode("utf-8")), expected)

    def test_same_key_in_different_objects_is_fine(self):
        self.assertEqual(strict_json.loads_text('{"a": {"x": 1}, "b": {"x": 2}, "c": [{"x": 3}, {"x": 4}]}')["c"][1], {"x": 4})

    def test_load_path_roundtrip(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "ok.json"
            p.write_text('{"k": "v"}', encoding="utf-8")
            self.assertEqual(strict_json.load_path(p), {"k": "v"})
            self.assertEqual(strict_json.load_path(str(p)), {"k": "v"})

    def test_top_level_non_object_only_when_allowed(self):
        self.assertEqual(code_of(strict_json.loads_text, "[1, 2]"), "not_object")
        self.assertEqual(code_of(strict_json.loads_text, "1"), "not_object")
        self.assertEqual(code_of(strict_json.loads_text, '"s"'), "not_object")
        self.assertEqual(strict_json.loads_text("[1, 2]", require_object=False), [1, 2])


class DuplicateKeyTests(NoNetworkCase):
    def test_duplicate_at_any_depth(self):
        for text in (
            '{"a": 1, "a": 2}',
            '{"x": {"a": 1, "a": 2}}',
            '{"x": [{"ok": 1}, {"a": 1, "a": 1}]}',
            '{"x": {"y": {"z": {"a": 1, "b": 2, "a": 3}}}}',
            '{"sources": {"s": {"status": "disabled"}, "s": {"status": "approved"}}}',
        ):
            with self.subTest(text=text[:40]):
                self.assertEqual(code_of(strict_json.loads_text, text), "duplicate_key")

    def test_escaped_equivalent_keys_are_duplicates(self):
        self.assertEqual(code_of(strict_json.loads_text, '{"a": 1, "\\u0061": 2}'), "duplicate_key")
        self.assertEqual(code_of(strict_json.loads_text, '{"\\ud55c": 1, "한": 2}'), "duplicate_key")

    def test_plain_json_loads_would_accept_these(self):
        # 회귀 근거: 표준 json.loads 는 마지막 값을 조용히 채택한다.
        self.assertEqual(json.loads('{"a": "disabled", "a": "approved"}')["a"], "approved")


class NumberTests(NoNetworkCase):
    def test_non_standard_constants(self):
        for text in ('{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}', '[NaN]', '{"a": [1, Infinity]}'):
            with self.subTest(text=text):
                self.assertEqual(code_of(strict_json.loads_text, text, require_object=False), "non_standard_number")

    def test_overflow_to_infinity(self):
        for text in ('{"a": 1e999}', '{"a": -1e999}', '{"a": 1E400}'):
            with self.subTest(text=text):
                self.assertEqual(code_of(strict_json.loads_text, text), "non_standard_number")

    def test_integer_digit_limit(self):
        ok = '{"n": %s}' % ("9" * strict_json.MAX_INT_DIGITS)
        self.assertEqual(len(str(strict_json.loads_text(ok)["n"])), strict_json.MAX_INT_DIGITS)
        too_long = '{"n": %s}' % ("9" * (strict_json.MAX_INT_DIGITS + 1))
        self.assertEqual(code_of(strict_json.loads_text, too_long), "number_out_of_range")
        self.assertEqual(code_of(strict_json.loads_text, '{"n": -%s}' % ("9" * 65)), "number_out_of_range")

    def test_nan_text_inside_strings_is_fine(self):
        self.assertEqual(strict_json.loads_text('{"a": "NaN", "b": "Infinity"}'), {"a": "NaN", "b": "Infinity"})


class EncodingTests(NoNetworkCase):
    def test_bom_rejected(self):
        self.assertEqual(code_of(strict_json.loads_bytes, b"\xef\xbb\xbf{}"), "bom")
        self.assertEqual(code_of(strict_json.loads_text, "\ufeff{}"), "bom")
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "bom.json"
            p.write_bytes(b"\xef\xbb\xbf{}")
            self.assertEqual(code_of(strict_json.load_path, p), "bom")

    def test_invalid_utf8_rejected(self):
        for raw in (b'{"a": "\xff"}', b'{"a": "\xc3("}', b'{"a": "\xc0\xaf"}', b'{"a": "\xed\xa0\x80"}',
                    '{"a": "x"}'.encode("utf-16"), '{"a": "x"}'.encode("utf-16-le")):
            with self.subTest(raw=raw[:12]):
                self.assertIn(code_of(strict_json.loads_bytes, raw), ("bad_encoding", "bom", "invalid_json"))

    def test_lone_surrogates_rejected_but_pairs_allowed(self):
        self.assertEqual(code_of(strict_json.loads_text, '{"a": "\\ud800"}'), "invalid_string")
        self.assertEqual(code_of(strict_json.loads_text, '{"\\udc00": 1}'), "invalid_string")
        self.assertEqual(code_of(strict_json.loads_text, '{"a": ["x", {"b": "\\udfff"}]}'), "invalid_string")
        self.assertEqual(strict_json.loads_text('{"a": "\\ud83d\\ude00"}')["a"], "\U0001F600")

    def test_bad_input_types(self):
        for func in (strict_json.loads_bytes, strict_json.loads_text):
            for bad in (None, 5, [], {}, object()):
                with self.subTest(func=func.__name__, bad=type(bad).__name__):
                    self.assertEqual(code_of(func, bad), "bad_input")
        self.assertEqual(code_of(strict_json.loads_bytes, "{}"), "bad_input")
        self.assertEqual(code_of(strict_json.loads_text, b"{}"), "bad_input")


class SizeTests(NoNetworkCase):
    @staticmethod
    def padded(size):
        head, tail = b'{"a": "', b'"}'
        return head + b"x" * (size - len(head) - len(tail)) + tail

    def test_exactly_max_bytes_is_accepted(self):
        data = self.padded(MAX_BYTES)
        self.assertEqual(len(data), MAX_BYTES)
        self.assertEqual(len(strict_json.loads_bytes(data)["a"]), MAX_BYTES - 9)

    def test_one_byte_over_is_rejected_before_parsing(self):
        data = self.padded(MAX_BYTES + 1)
        with mock.patch("strict_json.json.loads", side_effect=AssertionError("must not parse")):
            self.assertEqual(code_of(strict_json.loads_bytes, data), "too_large")

    def test_text_size_counts_utf8_bytes(self):
        text = '{"a": "' + "한" * 400_000 + '"}'  # 문자 수는 적지만 UTF-8로는 1 MiB 초과
        self.assertLess(len(text), MAX_BYTES)
        self.assertEqual(code_of(strict_json.loads_text, text), "too_large")

    def test_load_path_reads_at_most_limit_plus_one(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "big.json"
            p.write_bytes(self.padded(MAX_BYTES + 5000))
            self.assertEqual(code_of(strict_json.load_path, p), "too_large")

    def test_custom_lower_limits_only(self):
        self.assertEqual(code_of(strict_json.loads_text, '{"a": "0123456789"}', max_bytes=10), "too_large")
        with self.assertRaises(ValueError):
            strict_json.loads_text("{}", max_bytes=MAX_BYTES + 1)
        with self.assertRaises(ValueError):
            strict_json.loads_text("{}", max_depth=MAX_DEPTH + 1)
        with self.assertRaises(ValueError):
            strict_json.loads_text("{}", max_bytes=0)


class DepthTests(NoNetworkCase):
    def test_depth_32_ok_33_rejected(self):
        nested_ok = '{"a": ' + "[" * (MAX_DEPTH - 1) + "]" * (MAX_DEPTH - 1) + "}"  # 객체 1 + 배열 31 = 32
        self.assertIsInstance(strict_json.loads_text(nested_ok), dict)
        nested_bad = '{"a": ' + "[" * MAX_DEPTH + "]" * MAX_DEPTH + "}"  # 33
        self.assertEqual(code_of(strict_json.loads_text, nested_bad), "too_deep")

    def test_objects_only_nesting(self):
        ok = '{"a":' * (MAX_DEPTH - 1) + "{}" + "}" * (MAX_DEPTH - 1)
        self.assertIsInstance(strict_json.loads_text(ok), dict)
        bad = '{"a":' * MAX_DEPTH + "{}" + "}" * MAX_DEPTH
        self.assertEqual(code_of(strict_json.loads_text, bad), "too_deep")

    def test_extremely_deep_input_does_not_crash_the_interpreter(self):
        text = "[" * 200_000
        self.assertEqual(code_of(strict_json.loads_text, text, require_object=False), "too_deep")
        text = '{"a":' * 100_000
        self.assertEqual(code_of(strict_json.loads_text, text), "too_deep")

    def test_brackets_inside_strings_do_not_count(self):
        text = json.dumps({"a": "[" * 100 + "{" * 100, "b": ['\\"[[[[', "]]]]"]})
        self.assertIsInstance(strict_json.loads_text(text), dict)

    def test_escaped_quote_does_not_end_the_string_for_depth_counting(self):
        text = '{"a": "\\"' + "[" * 100 + '\\"", "b": 1}'
        self.assertEqual(strict_json.loads_text(text)["b"], 1)

    def test_mismatched_brackets_are_invalid_json_not_a_crash(self):
        self.assertEqual(code_of(strict_json.loads_text, "]]]]{}", require_object=False), "invalid_json")


class MalformedTests(NoNetworkCase):
    def test_invalid_json(self):
        for text in ('{"a": 1,}', "{'a': 1}", '{"a": 1} // c', '/* c */ {}', '{"a": 1} {"b": 2}', '{"a" 1}', '{a: 1}',
                     '{"a": 01}', '{"a": "line\nbreak"}', '{"a": "tab\there"}', '{"a": .5}', '{"a": +1}', '{"a": 0x10}',
                     '{"a": tru}', '{"a": "\\x41"}', '{"a": 1', '{"a": [1, 2}', '\x00{}'):
            with self.subTest(text=text[:25]):
                self.assertEqual(code_of(strict_json.loads_text, text), "invalid_json")

    def test_empty_and_whitespace(self):
        for text in ("", "   ", "\n\t "):
            self.assertEqual(code_of(strict_json.loads_text, text), "empty")
            self.assertEqual(code_of(strict_json.loads_bytes, text.encode()), "empty")


class ErrorHygieneTests(NoNetworkCase):
    """오류에 입력 값·키 이름·경로가 남지 않고, 예외 체인도 원본 문서를 담지 않는다."""

    def assert_clean(self, err, forbidden):
        shown = [str(err), repr(err), repr(err.args), err.code]
        for text in shown:
            for needle in forbidden:
                self.assertNotIn(needle, text)
        self.assertIsNone(err.__cause__)
        self.assertIsNone(err.__context__)
        self.assertIn(err.code, strict_json.CODES)
        self.assertEqual(str(err), err.code)

    def test_text_errors_do_not_echo_input(self):
        secret_key = "KEYNAME_SECRET"
        docs = [
            '{"%s": "%s", "%s": 1}' % (secret_key, SECRET, secret_key),
            '{"%s": "%s",}' % (secret_key, SECRET),
            '{"%s": NaN, "v": "%s"}' % (secret_key, SECRET),
            '{"%s": "\\ud800 %s"}' % (secret_key, SECRET),
            '{"%s": 1e999, "v": "%s"}' % (secret_key, SECRET),
            ('{"%s": ' % secret_key) * 40 + '"%s"' % SECRET,
        ]
        for doc in docs:
            with self.subTest(doc=doc[:30]):
                with self.assertRaises(StrictJsonError) as cm:
                    strict_json.loads_text(doc)
                self.assert_clean(cm.exception, [SECRET, secret_key])

    def test_bytes_errors_do_not_echo_input(self):
        for raw in (b'{"k": "' + SECRET.encode() + b'\xff"}', b"\xef\xbb\xbf{\"k\": \"" + SECRET.encode() + b'"}'):
            with self.assertRaises(StrictJsonError) as cm:
                strict_json.loads_bytes(raw)
            self.assert_clean(cm.exception, [SECRET])

    def test_path_errors_do_not_expose_the_path(self):
        with tempfile.TemporaryDirectory(prefix="PATHMARKER_") as t:
            missing = Path(t) / "missing_FILEMARKER.json"
            with self.assertRaises(StrictJsonError) as cm:
                strict_json.load_path(missing)
            self.assertEqual(cm.exception.code, "read_failed")
            self.assert_clean(cm.exception, ["PATHMARKER_", "FILEMARKER"])
            with self.assertRaises(StrictJsonError) as cm:
                strict_json.load_path(Path(t))  # 디렉터리
            self.assert_clean(cm.exception, ["PATHMARKER_"])
            bad = Path(t) / "bad_FILEMARKER.json"
            bad.write_text('{"a": 1, "a": 2, "s": "%s"}' % SECRET, encoding="utf-8")
            with self.assertRaises(StrictJsonError) as cm:
                strict_json.load_path(bad)
            self.assertEqual(cm.exception.code, "duplicate_key")
            self.assert_clean(cm.exception, ["PATHMARKER_", "FILEMARKER", SECRET])
            for bad_path in (None, 5, "\x00bad"):
                with self.assertRaises(StrictJsonError) as cm:
                    strict_json.load_path(bad_path)
                self.assertEqual(cm.exception.code, "read_failed")

    def test_all_codes_are_documented(self):
        self.assertEqual(len(set(strict_json.CODES)), len(strict_json.CODES))


class StaticTests(unittest.TestCase):
    def test_standard_library_only_and_no_network_modules(self):
        tree = ast.parse((ROOT / "strict_json.py").read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
        self.assertEqual(names - {"__future__", "json", "re", "pathlib"}, set())


if __name__ == "__main__":
    unittest.main()

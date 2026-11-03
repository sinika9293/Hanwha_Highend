"""robots.txt 매처 테스트.

주의: 아래 RFC 9309 예제(그룹 예제, 경로 매칭 표, 최장 일치 규칙)는 사양의 내용을 바탕으로 재구성한 것이며, 이번 작업에서는
외부 접근이 금지되어 RFC 원문과 글자 단위로 대조하지 못했다(UNVERIFIED). 실제 판정 의미(최장 일치, `*`, `$`, 그룹 병합,
동률 시 Allow)를 검증한다.
"""

import ast
import socket
import time
import unittest
from pathlib import Path
from unittest import mock

import robots_matcher as rm
from robots_matcher import RobotsError

ROOT = Path(__file__).resolve().parent.parent
AGENT = "MUWA-source-fetch"


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

    def allowed(self, text, target, agent=AGENT):
        return rm.parse(text).is_allowed(agent, target)

    def error_code(self, func, *args, **kwargs):
        with self.assertRaises(RobotsError) as cm:
            func(*args, **kwargs)
        return cm.exception.code


class PathMatchingTableTests(NoNetworkCase):
    """RFC 9309 2.2.2 / 2.2.3 의 경로 매칭 표 (대소문자 구분, 쿼리 포함, `*`, `$`)."""

    TABLE = (
        ("/", ("/", "/anything", "/a/b?c=d"), ()),
        ("/*", ("/", "/anything"), ()),
        ("/fish", ("/fish", "/fish.html", "/fish/salmon.html", "/fishheads", "/fishheads/yummy.html", "/fish.php?id=anything"),
         ("/Fish.asp", "/catfish", "/?id=fish", "/desert/fish")),
        ("/fish*", ("/fish", "/fish.html", "/fish/salmon.html", "/fishheads", "/fishheads/yummy.html", "/fish.php?id=anything"),
         ("/Fish.asp", "/catfish", "/?id=fish", "/desert/fish")),
        ("/fish/", ("/fish/", "/fish/?id=anything", "/fish/salmon.htm"), ("/fish", "/fish.html", "/Fish/Salmon.asp")),
        ("/*.php", ("/filename.php", "/folder/filename.php", "/folder/filename.php?parameters", "/folder/any.php.file.html", "/filename.php/"),
         ("/", "/windows.PHP")),
        ("/*.php$", ("/filename.php", "/folder/filename.php"),
         ("/filename.php?parameters", "/filename.php/", "/filename.php5", "/windows.PHP")),
        ("/fish*.php", ("/fish.php", "/fishheads/catfish.php?parameters"), ("/Fish.PHP",)),
    )

    def test_disallow_table(self):
        for pattern, blocked, passed in self.TABLE:
            robots = rm.parse("User-agent: *\nDisallow: %s\n" % pattern)
            for path in blocked:
                with self.subTest(pattern=pattern, path=path, expect="blocked"):
                    self.assertFalse(robots.is_allowed(AGENT, path))
            for path in passed:
                with self.subTest(pattern=pattern, path=path, expect="allowed"):
                    self.assertTrue(robots.is_allowed(AGENT, path))

    def test_allow_rules_use_the_same_matching(self):
        for pattern, matched, unmatched in self.TABLE:
            robots = rm.parse("User-agent: *\nDisallow: /\nAllow: %s\n" % pattern)
            if pattern in ("/", "/*"):
                continue  # 길이가 같은 Allow/Disallow 동률 -> 별도 테스트
            for path in matched:
                with self.subTest(pattern=pattern, path=path):
                    self.assertTrue(robots.is_allowed(AGENT, path))
            for path in unmatched:
                with self.subTest(pattern=pattern, path=path):
                    self.assertFalse(robots.is_allowed(AGENT, path))

    def test_dollar_is_special_only_at_the_end(self):
        robots = rm.parse("User-agent: *\nDisallow: /a$b\n")
        self.assertFalse(robots.is_allowed(AGENT, "/a$b"))
        self.assertFalse(robots.is_allowed(AGENT, "/a$bc"))
        self.assertTrue(robots.is_allowed(AGENT, "/a"))
        self.assertTrue(robots.is_allowed(AGENT, "/ab"))

    def test_multiple_wildcards_and_anchor(self):
        robots = rm.parse("User-agent: *\nDisallow: /a*b*c\nDisallow: /x*y$\n")
        self.assertFalse(robots.is_allowed(AGENT, "/aXXbYYc"))
        self.assertFalse(robots.is_allowed(AGENT, "/abc"))
        self.assertTrue(robots.is_allowed(AGENT, "/aXXcYYb"))
        self.assertFalse(robots.is_allowed(AGENT, "/xAAy"))
        self.assertFalse(robots.is_allowed(AGENT, "/xyy"))
        self.assertTrue(robots.is_allowed(AGENT, "/xAAyZ"))
        self.assertTrue(robots.is_allowed(AGENT, "/xA"))

    def test_query_string_is_part_of_the_match_and_fragment_is_not(self):
        robots = rm.parse("User-agent: *\nDisallow: /*?id=\nDisallow: /exact$\n")
        self.assertFalse(robots.is_allowed(AGENT, "/x?id=1"))
        self.assertTrue(robots.is_allowed(AGENT, "/x?other=1"))
        self.assertFalse(robots.is_allowed(AGENT, "/exact"))
        self.assertTrue(robots.is_allowed(AGENT, "/exact?x=1"))
        self.assertFalse(robots.is_allowed(AGENT, "/exact#frag"))  # fragment 는 비교에서 제외
        self.assertEqual(robots.evaluate(AGENT, "/exact#frag"), robots.evaluate(AGENT, "/exact"))

    def test_full_urls_are_reduced_to_path_and_query(self):
        robots = rm.parse("User-agent: *\nDisallow: /fish/\nDisallow: /*?secret=\n")
        self.assertFalse(robots.is_allowed(AGENT, "https://example.org/fish/salmon?x=1#f"))
        self.assertFalse(robots.is_allowed(AGENT, "http://example.org/page?secret=1"))
        self.assertTrue(robots.is_allowed(AGENT, "https://example.org/fish"))
        self.assertTrue(robots.is_allowed(AGENT, "https://example.org"))  # 경로 없음 = "/"
        self.assertEqual(self.allowed("User-agent: *\nDisallow: /\n", "https://example.org"), False)


class LongestMatchTests(NoNetworkCase):
    def test_longest_pattern_wins(self):
        robots = rm.parse("User-agent: *\nAllow: /example/page/\nDisallow: /example/page/disallowed.gif\nDisallow: /example/\n")
        self.assertTrue(robots.is_allowed(AGENT, "/example/page/"))
        self.assertTrue(robots.is_allowed(AGENT, "/example/page/other.html"))
        self.assertFalse(robots.is_allowed(AGENT, "/example/page/disallowed.gif"))
        self.assertFalse(robots.is_allowed(AGENT, "/example/other"))

    def test_rule_order_does_not_matter(self):
        a = rm.parse("User-agent: *\nAllow: /a/b\nDisallow: /a\n")
        b = rm.parse("User-agent: *\nDisallow: /a\nAllow: /a/b\n")
        for path in ("/a", "/a/b", "/a/b/c", "/a/c", "/z"):
            self.assertEqual(a.is_allowed(AGENT, path), b.is_allowed(AGENT, path), path)
        self.assertTrue(a.is_allowed(AGENT, "/a/b/c"))
        self.assertFalse(a.is_allowed(AGENT, "/a/c"))

    def test_equal_length_allow_wins(self):
        for text in ("Allow: /page\nDisallow: /page\n", "Disallow: /page\nAllow: /page\n", "Disallow: /a*b\nAllow: /a*b\n",
                     "Disallow: /a/b/c\nAllow: /a/*/c\n"):
            with self.subTest(text=text):
                robots = rm.parse("User-agent: *\n" + text)
                self.assertTrue(robots.is_allowed(AGENT, "/page") or robots.is_allowed(AGENT, "/aXb") or robots.is_allowed(AGENT, "/a/b/c"))
        robots = rm.parse("User-agent: *\nDisallow: /page\nAllow: /page\n")
        self.assertTrue(robots.is_allowed(AGENT, "/page"))
        decision = robots.evaluate(AGENT, "/page")
        self.assertTrue(decision.rule.allow)

    def test_pattern_length_counts_wildcards_and_anchor(self):
        # "/a*" (3) 보다 "/a/b" (4)가 더 구체적이다.
        robots = rm.parse("User-agent: *\nAllow: /a*\nDisallow: /a/b\n")
        self.assertFalse(robots.is_allowed(AGENT, "/a/b/c"))
        self.assertTrue(robots.is_allowed(AGENT, "/ax"))
        # "$" 도 길이에 포함: "/p$" (3) 대 "/p" (2)
        robots = rm.parse("User-agent: *\nDisallow: /p\nAllow: /p$\n")
        self.assertTrue(robots.is_allowed(AGENT, "/p"))
        self.assertFalse(robots.is_allowed(AGENT, "/pq"))

    def test_no_matching_rule_means_allowed(self):
        robots = rm.parse("User-agent: *\nDisallow: /private\n")
        decision = robots.evaluate(AGENT, "/public")
        self.assertTrue(decision.allowed)
        self.assertIsNone(decision.rule)
        self.assertEqual(decision.group, "wildcard")


class GroupTests(NoNetworkCase):
    EXAMPLE = (
        "User-Agent: *\nDisallow: *.gif$\nDisallow: /example/\nAllow: /publications/\n\n"
        "User-Agent: foobot\nDisallow:/\nAllow:/example/page.html\nAllow:/example/allowed.gif\n\n"
        "User-Agent: barbot\nUser-Agent: bazbot\nDisallow: /example/page.html\n\n"
        "User-Agent: quxbot\n"
    )

    def test_rfc_style_group_example(self):
        robots = rm.parse(self.EXAMPLE)
        # 일반 크롤러: '*' 그룹
        for path, expected in (("/example/foo", False), ("/example/page.html", False), ("/a.gif", False), ("/publications/x", True),
                               ("/publications/a.gif", True), ("/other", True), ("/", True)):
            with self.subTest(agent="otherbot", path=path):
                self.assertEqual(robots.is_allowed("otherbot", path), expected)
        # foobot: 자기 그룹만 (루트 전체 차단, 두 예외 허용)
        for path, expected in (("/", False), ("/anything", False), ("/example/page.html", True), ("/example/allowed.gif", True),
                               ("/example/other.html", False)):
            with self.subTest(agent="foobot", path=path):
                self.assertEqual(robots.is_allowed("foobot", path), expected)
        # barbot / bazbot: 연속된 user-agent 줄이 하나의 그룹을 공유, '*' 규칙과 병합되지 않는다
        for bot in ("barbot", "bazbot"):
            self.assertFalse(robots.is_allowed(bot, "/example/page.html"))
            self.assertTrue(robots.is_allowed(bot, "/example/other"))
            self.assertTrue(robots.is_allowed(bot, "/a.gif"))
        # quxbot: 규칙이 없는 자기 그룹이 있으므로 '*' 로 떨어지지 않고 모두 허용
        self.assertTrue(robots.is_allowed("quxbot", "/example/foo"))
        self.assertTrue(robots.is_allowed("quxbot", "/a.gif"))
        self.assertEqual(robots.evaluate("quxbot", "/x").group, "specific")
        self.assertEqual(robots.evaluate("otherbot", "/x").group, "wildcard")

    def test_agent_matching_is_case_insensitive_and_accepts_a_version_suffix(self):
        robots = rm.parse("User-agent: MUWA-SOURCE-FETCH\nDisallow: /x\n")
        for agent in ("MUWA-source-fetch", "muwa-source-fetch", "MUWA-Source-Fetch/1.0", "  MUWA-source-fetch/2.3 (+contact)  "):
            with self.subTest(agent=agent):
                self.assertFalse(robots.is_allowed(agent, "/x"))
        self.assertTrue(robots.is_allowed("MUWA", "/x"))  # 제품 토큰 전체가 같아야 한다 (접두사 일치 아님)
        self.assertTrue(robots.is_allowed("muwa-source-fetcher", "/x"))

    def test_groups_for_the_same_agent_are_merged(self):
        robots = rm.parse("User-agent: muwa-source-fetch\nDisallow: /a\n\nUser-agent: other\nDisallow: /x\n\n"
                          "User-agent: MUWA-SOURCE-FETCH\nDisallow: /b\n")
        self.assertFalse(robots.is_allowed(AGENT, "/a"))
        self.assertFalse(robots.is_allowed(AGENT, "/b"))
        self.assertTrue(robots.is_allowed(AGENT, "/x"))
        self.assertEqual(len(robots.groups), 3)

    def test_specific_group_replaces_wildcard_group(self):
        robots = rm.parse("User-agent: *\nDisallow: /\n\nUser-agent: MUWA-source-fetch\nAllow: /public/\nDisallow: /\n")
        self.assertTrue(robots.is_allowed(AGENT, "/public/a"))
        self.assertFalse(robots.is_allowed(AGENT, "/private"))
        robots = rm.parse("User-agent: *\nDisallow: /\n\nUser-agent: MUWA-source-fetch\nDisallow:\n")
        self.assertTrue(robots.is_allowed(AGENT, "/anything"))
        self.assertFalse(robots.is_allowed("other", "/anything"))

    def test_multiple_wildcard_groups_are_merged(self):
        robots = rm.parse("User-agent: *\nDisallow: /a\n\nUser-agent: *\nDisallow: /b\n")
        self.assertFalse(robots.is_allowed(AGENT, "/a"))
        self.assertFalse(robots.is_allowed(AGENT, "/b"))

    def test_no_matching_group_and_no_wildcard_means_allowed(self):
        robots = rm.parse("User-agent: otherbot\nDisallow: /\n")
        decision = robots.evaluate(AGENT, "/x")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.group, "none")
        self.assertTrue(rm.parse("").is_allowed(AGENT, "/x"))
        self.assertTrue(rm.parse("# only a comment\n").is_allowed(AGENT, "/x"))

    def test_group_boundaries(self):
        # 빈 줄·주석·sitemap·알 수 없는 줄은 그룹을 끊지 않는다
        robots = rm.parse("User-agent: a\n\n# comment\nSitemap: https://example.org/s.xml\nUnknown: x\nUser-agent: b\n\nDisallow: /z\n")
        self.assertFalse(robots.is_allowed("a", "/z"))
        self.assertFalse(robots.is_allowed("b", "/z"))
        # 규칙 뒤의 user-agent 는 새 그룹
        robots = rm.parse("User-agent: a\nDisallow: /one\nUser-agent: b\nDisallow: /two\n")
        self.assertFalse(robots.is_allowed("a", "/one"))
        self.assertTrue(robots.is_allowed("a", "/two"))
        self.assertFalse(robots.is_allowed("b", "/two"))
        self.assertTrue(robots.is_allowed("b", "/one"))
        # 첫 user-agent 앞의 규칙은 무시
        robots = rm.parse("Disallow: /orphan\nUser-agent: *\nDisallow: /real\n")
        self.assertTrue(robots.is_allowed(AGENT, "/orphan"))
        self.assertFalse(robots.is_allowed(AGENT, "/real"))
        # 값이 없는 user-agent 줄은 무시
        robots = rm.parse("User-agent:\nDisallow: /x\n")
        self.assertTrue(robots.is_allowed(AGENT, "/x"))

    def test_empty_disallow_and_allow(self):
        self.assertTrue(self.allowed("User-agent: *\nDisallow:\n", "/anything"))
        self.assertFalse(self.allowed("User-agent: *\nDisallow: /\nAllow:\n", "/anything"))
        self.assertTrue(self.allowed("User-agent: *\nDisallow: /a\nDisallow:\n", "/zzz"))
        self.assertFalse(self.allowed("User-agent: *\nDisallow: /a\nDisallow:\n", "/a"))


class SyntaxTests(NoNetworkCase):
    def test_directive_case_whitespace_comments_and_line_endings(self):
        for text in ("USER-AGENT: *\nDISALLOW: /x\n", "uSeR-aGeNt : *\n  dIsAlLoW :   /x   # trailing comment\n",
                     "User-agent: *\r\nDisallow: /x\r\n", "User-agent: *\rDisallow: /x\r", "User-agent: *\nDisallow: /x",
                     "\ufeffUser-agent: *\nDisallow: /x\n", "# header\n\nUser-agent: * # who\nDisallow: /x\n"):
            with self.subTest(text=repr(text)[:40]):
                robots = rm.parse(text)
                self.assertFalse(robots.is_allowed(AGENT, "/x"))
                self.assertTrue(robots.is_allowed(AGENT, "/y"))

    def test_bytes_input_and_invalid_utf8(self):
        robots = rm.parse(b"\xef\xbb\xbfUser-agent: *\nDisallow: /x\n")
        self.assertFalse(robots.is_allowed(AGENT, "/x"))
        robots = rm.parse(b"User-agent: *\n# caf\xe9 \xff comment\nDisallow: /x\n")  # 잘못된 UTF-8 은 대체 문자로 처리, 규칙은 유지
        self.assertFalse(robots.is_allowed(AGENT, "/x"))
        self.assertEqual(rm.parse(bytearray(b"User-agent: *\nDisallow: /x\n")), rm.parse("User-agent: *\nDisallow: /x\n"))

    def test_lines_without_colon_and_unknown_directives_are_ignored(self):
        robots = rm.parse("User-agent: *\nDisallow /x\nDissalowed: /y\nNoindex: /z\nDisallow: /w\n")
        self.assertTrue(robots.is_allowed(AGENT, "/x"))
        self.assertTrue(robots.is_allowed(AGENT, "/y"))
        self.assertFalse(robots.is_allowed(AGENT, "/w"))

    def test_conservative_handling_of_patterns_without_leading_slash(self):
        self.assertFalse(self.allowed("User-agent: *\nDisallow: private\n", "/private/x"))
        self.assertTrue(self.allowed("User-agent: *\nDisallow: /\nAllow: public\n", "/public") is False)
        self.assertFalse(self.allowed("User-agent: *\nDisallow: *.gif$\n", "/a.gif"))
        self.assertTrue(self.allowed("User-agent: *\nAllow: *.gif$\nDisallow: /\n", "/a.gif"))

    def test_deterministic(self):
        text = TestFixtures.SAMPLE
        self.assertEqual(rm.parse(text), rm.parse(text))


class PercentEncodingTests(NoNetworkCase):
    def test_unreserved_escapes_are_equivalent_to_literals(self):
        self.assertFalse(self.allowed("User-agent: *\nDisallow: /a%62c\n", "/abc"))
        self.assertFalse(self.allowed("User-agent: *\nDisallow: /abc\n", "/a%62c"))
        self.assertFalse(self.allowed("User-agent: *\nDisallow: /%7Euser\n", "/~user"))
        self.assertFalse(self.allowed("User-agent: *\nDisallow: /~user\n", "/%7euser"))
        self.assertFalse(self.allowed("User-agent: *\nDisallow: /%41\n", "/A"))

    def test_hex_digits_case_insensitive_and_reserved_not_decoded(self):
        self.assertFalse(self.allowed("User-agent: *\nDisallow: /a%2fb\n", "/a%2Fb"))
        self.assertTrue(self.allowed("User-agent: *\nDisallow: /a%2fb\n", "/a/b"))
        self.assertTrue(self.allowed("User-agent: *\nDisallow: /a/b\n", "/a%2Fb"))
        self.assertFalse(self.allowed("User-agent: *\nDisallow: /%24\n", "/%24"))

    def test_non_ascii_paths(self):
        encoded = "/%ED%95%9C%EA%B8%80"
        self.assertFalse(self.allowed("User-agent: *\nDisallow: /한글\n", encoded))
        self.assertFalse(self.allowed("User-agent: *\nDisallow: /%ed%95%9c%ea%b8%80\n", "/한글"))
        self.assertTrue(self.allowed("User-agent: *\nDisallow: /한글\n", "/한국"))

    def test_invalid_percent_sequences_are_compared_literally(self):
        robots = rm.parse("User-agent: *\nDisallow: /a%zz\n")
        self.assertFalse(robots.is_allowed(AGENT, "/a%zz"))
        self.assertTrue(robots.is_allowed(AGENT, "/a"))
        self.assertFalse(self.allowed("User-agent: *\nDisallow: /100%\n", "/100%"))

    def test_normalize_path_function(self):
        self.assertEqual(rm.normalize_path("/a%62c%2f%7e/한"), "/abc%2F~/%ED%95%9C")
        self.assertEqual(rm.normalize_path("%"), "%25")


class CrawlDelayTests(NoNetworkCase):
    def test_crawl_delay_is_separate_from_allow_and_deny(self):
        with_delay = rm.parse("User-agent: *\nCrawl-delay: 10\nDisallow: /x\nAllow: /x/ok\n")
        without = rm.parse("User-agent: *\nDisallow: /x\nAllow: /x/ok\n")
        for path in ("/x", "/x/ok", "/x/ok/y", "/other"):
            self.assertEqual(with_delay.is_allowed(AGENT, path), without.is_allowed(AGENT, path), path)
        self.assertEqual(with_delay.crawl_delay(AGENT), 10.0)
        self.assertIsNone(without.crawl_delay(AGENT))
        # Crawl-delay 줄이 user-agent 연속 구간이나 그룹 경계를 바꾸지 않는다
        shared = rm.parse("User-agent: a\nCrawl-delay: 5\nUser-agent: b\nDisallow: /q\n")
        for bot in ("a", "b"):
            self.assertFalse(shared.is_allowed(bot, "/q"))
            self.assertEqual(shared.crawl_delay(bot), 5.0)

    def test_invalid_values_are_ignored(self):
        for value in ("abc", "-1", "inf", "nan", "1e999", "90000", "", "1,5"):
            with self.subTest(value=value):
                self.assertIsNone(rm.parse("User-agent: *\nCrawl-delay: %s\n" % value).crawl_delay(AGENT))
        self.assertEqual(rm.parse("User-agent: *\nCrawl-delay: 0\n").crawl_delay(AGENT), 0.0)
        self.assertEqual(rm.parse("User-agent: *\nCrawl-delay: 2.5\n").crawl_delay(AGENT), 2.5)

    def test_selection_and_merging_follow_the_group_rules(self):
        robots = rm.parse("User-agent: *\nCrawl-delay: 30\nDisallow: /w\n\nUser-agent: muwa-source-fetch\nCrawl-delay: 4\nDisallow: /a\n\n"
                          "User-agent: MUWA-source-fetch\nCrawl-delay: 9\nDisallow: /b\n")
        self.assertEqual(robots.crawl_delay(AGENT), 9.0)  # 특정 그룹들만 병합, 가장 큰 값
        self.assertEqual(robots.crawl_delay("other"), 30.0)  # '*' 그룹
        # Crawl-delay 만 있고 규칙이 없는 구간은 user-agent 연속 구간을 끊지 않는다(비표준 줄은 정의된 레코드의 파싱을 방해하지 않는다).
        merged = rm.parse("User-agent: *\nCrawl-delay: 30\n\nUser-agent: muwa-source-fetch\nCrawl-delay: 4\nDisallow: /a\n")
        self.assertEqual(len(merged.groups), 1)
        self.assertEqual(merged.groups[0].agents, ("*", "muwa-source-fetch"))
        plain = rm.parse("User-agent: *\n\nUser-agent: muwa-source-fetch\nDisallow: /a\n")
        for path in ("/a", "/b"):
            self.assertEqual(merged.is_allowed(AGENT, path), plain.is_allowed(AGENT, path))
        self.assertIsNone(rm.parse("User-agent: x\nCrawl-delay: 3\n").crawl_delay(AGENT))
        self.assertIsNone(rm.parse("Crawl-delay: 3\nUser-agent: *\nDisallow: /a\n").crawl_delay(AGENT))


class LimitsAndFailClosedTests(NoNetworkCase):
    @staticmethod
    def padded(size):
        head = b"User-agent: *\nDisallow: /x\n"
        filler = b"#" + b"y" * 78 + b"\n"
        body = head + filler * ((size - len(head)) // len(filler))
        return body + b"#" * (size - len(body))

    def test_size_boundary(self):
        data = self.padded(rm.MAX_BYTES)
        self.assertEqual(len(data), rm.MAX_BYTES)
        self.assertFalse(rm.parse(data).is_allowed(AGENT, "/x"))
        self.assertEqual(self.error_code(rm.parse, data + b"#"), "too_large")
        self.assertEqual(self.error_code(rm.parse, data.decode("ascii") + "#"), "too_large")

    def test_text_size_counts_utf8_bytes(self):
        text = "# " + "한" * 200_000 + "\nUser-agent: *\nDisallow: /x\n"
        self.assertLess(len(text), rm.MAX_BYTES)
        self.assertEqual(self.error_code(rm.parse, text), "too_large")

    def test_oversized_input_is_rejected_whole_not_truncated(self):
        # 뒤쪽의 Disallow 규칙이 잘려 나가 허용 범위가 넓어지는 일이 없도록 파일 전체를 거부한다.
        data = b"User-agent: *\n" + b"#" + b"y" * rm.MAX_BYTES + b"\nDisallow: /secret\n"
        self.assertEqual(self.error_code(rm.parse, data), "too_large")

    def test_pattern_length_limit(self):
        ok = "User-agent: *\nDisallow: /" + "a" * (rm.MAX_PATTERN_LEN - 1) + "\n"
        self.assertEqual(len(rm.parse(ok).groups[0].rules[0].pattern), rm.MAX_PATTERN_LEN)
        bad = "User-agent: *\nDisallow: /x\nDisallow: /" + "a" * rm.MAX_PATTERN_LEN + "\n"
        self.assertEqual(self.error_code(rm.parse, bad), "pattern_too_long")
        bad_allow = "User-agent: *\nAllow: /" + "a" * rm.MAX_PATTERN_LEN + "\n"
        self.assertEqual(self.error_code(rm.parse, bad_allow), "pattern_too_long")

    def test_bad_inputs_raise_instead_of_allowing(self):
        for bad in (None, 5, [], {}, object()):
            self.assertEqual(self.error_code(rm.parse, bad), "bad_input")
        robots = rm.parse("User-agent: *\nDisallow: /x\n")
        for agent in (None, 5, "", "   ", "*", "123bot", "bot@example", "봇", "a" * 65):
            with self.subTest(agent=repr(agent)[:20]):
                self.assertEqual(self.error_code(robots.is_allowed, agent, "/x"), "bad_agent")
        for target in (None, 5, "", "x", "relative/path", "ftp://example.org/x", "file:///x", "/a b", "/a\nb", "/a\x00b", "/" + "x" * 9000):
            with self.subTest(target=repr(target)[:20]):
                self.assertEqual(self.error_code(robots.is_allowed, AGENT, target), "bad_target")
        self.assertEqual(self.error_code(robots.crawl_delay, "*"), "bad_agent")

    def test_pathological_patterns_finish_quickly(self):
        pattern = "/" + "*a" * 1500 + "*b$"
        robots = rm.parse("User-agent: *\nDisallow: %s\n" % pattern)
        started = time.monotonic()
        self.assertTrue(robots.is_allowed(AGENT, "/" + "a" * 3000))
        self.assertTrue(robots.is_allowed(AGENT, "/" + "a" * 3000 + "c"))
        self.assertFalse(robots.is_allowed(AGENT, "/" + "a" * 1500 + "b"))
        self.assertLess(time.monotonic() - started, 5.0)

    def test_many_rules(self):
        lines = ["User-agent: *"] + ["Disallow: /dir%d/" % i for i in range(20000)]
        robots = rm.parse("\n".join(lines) + "\n")
        self.assertFalse(robots.is_allowed(AGENT, "/dir19999/x"))
        self.assertTrue(robots.is_allowed(AGENT, "/dir20000/x"))


class SegmentPositionTests(NoNetworkCase):
    """리뷰 T4/T5: 와일드카드 사이 조각의 위치 제약 (구현은 올바르지만 기존 테스트가 잡지 못했던 경계)."""

    def blocked(self, pattern, path):
        return not rm.parse("User-agent: *\nDisallow: %s\n" % pattern).is_allowed(AGENT, path)

    def test_middle_segment_must_occur_after_the_previous_segment(self):
        self.assertFalse(self.blocked("/ab*b*c", "/abXc"))  # 가운데 'b'는 첫 조각 "/ab" 안에만 있다
        self.assertTrue(self.blocked("/ab*b*c", "/abXbYc"))
        self.assertTrue(self.blocked("/ab*b*c", "/abbc"))
        self.assertFalse(self.blocked("/a*a*a", "/aa"))  # 'a'가 세 번 필요하다
        self.assertTrue(self.blocked("/a*a*a", "/aaa"))
        self.assertTrue(self.blocked("/a*a*a", "/aXaYa"))
        self.assertFalse(self.blocked("/a*b*a*b", "/aba"))  # 조각이 겹쳐 쓰일 수 없다
        self.assertTrue(self.blocked("/a*b*a*b", "/abab"))  # 별표는 빈 문자열이어도 된다
        self.assertTrue(self.blocked("/a*b*a*b", "/aXbXaXb"))

    def test_anchored_last_segment_cannot_overlap_earlier_segments(self):
        self.assertFalse(self.blocked("/a*ab$", "/ab"))  # "/a" 다음에 "ab"가 따로 와야 한다
        self.assertTrue(self.blocked("/a*ab$", "/aXab"))
        self.assertTrue(self.blocked("/a*ab$", "/aab"))
        self.assertFalse(self.blocked("/ab*bc$", "/abc"))
        self.assertTrue(self.blocked("/ab*bc$", "/abbc"))
        self.assertFalse(self.blocked("/x*x$", "/x"))
        self.assertTrue(self.blocked("/x*x$", "/xx"))

    def test_unanchored_last_segment_still_needs_to_follow(self):
        self.assertFalse(self.blocked("/a*ab", "/ab"))
        self.assertTrue(self.blocked("/a*ab", "/aab"))
        self.assertTrue(self.blocked("/a*ab", "/aabYYY"))


class CrawlDelayFormatTests(NoNetworkCase):
    """리뷰 T6 + L3: Crawl-delay 는 ASCII 숫자 형식만, 같은 그룹의 여러 줄은 최댓값."""

    def delay(self, value, agent=AGENT):
        return rm.parse("User-agent: *\nCrawl-delay: %s\n" % value).crawl_delay(agent)

    def test_accepted_formats(self):
        for value, expected in (("0", 0.0), ("5", 5.0), ("05", 5.0), ("2.5", 2.5), ("0.5", 0.5), ("86400", 86400.0), ("86400.0", 86400.0),
                                ("  7  ", 7.0)):
            with self.subTest(value=value):
                self.assertEqual(self.delay(value), expected)

    def test_rejected_formats(self):
        for value in ("1_0", "٣", "١٢", "５", "1e2", "1E2", "+5", "-5", "-0", "5.", ".5", "0x10", "5s", "86400.1", "86401",
                      "1,5", "inf", "nan", "infinity", "", "1 2", "1.2.3", "½"):
            with self.subTest(value=ascii(value)):
                self.assertIsNone(self.delay(value))

    def test_multiple_lines_in_one_group_use_the_maximum(self):
        self.assertEqual(rm.parse("User-agent: *\nCrawl-delay: 3\nCrawl-delay: 9\nCrawl-delay: 4\n").crawl_delay(AGENT), 9.0)
        self.assertEqual(rm.parse("User-agent: *\nCrawl-delay: 9\nCrawl-delay: 3\n").crawl_delay(AGENT), 9.0)
        self.assertEqual(rm.parse("User-agent: *\nCrawl-delay: 9\nCrawl-delay: abc\nCrawl-delay: 1e9\nCrawl-delay: 3\n").crawl_delay(AGENT), 9.0)
        self.assertEqual(rm.parse("User-agent: *\nCrawl-delay: 2.5\nDisallow: /x\nCrawl-delay: 2.75\n").crawl_delay(AGENT), 2.75)

    def test_delay_before_any_user_agent_is_ignored(self):
        robots = rm.parse("Crawl-delay: 30\nUser-agent: *\nCrawl-delay: 2\n")
        self.assertEqual(robots.crawl_delay(AGENT), 2.0)
        self.assertIsNone(rm.parse("Crawl-delay: 30\n").crawl_delay(AGENT))

    def test_delay_never_changes_allow_decisions(self):
        text = "User-agent: *\nDisallow: /x\nAllow: /x/ok\n"
        for delay_lines in ("", "Crawl-delay: 5\n", "Crawl-delay: bogus\n"):
            robots = rm.parse(text.replace("Disallow", delay_lines + "Disallow", 1))
            self.assertFalse(robots.is_allowed(AGENT, "/x"))
            self.assertTrue(robots.is_allowed(AGENT, "/x/ok"))


class TypoAliasTests(NoNetworkCase):
    """리뷰 M1: 명시한 별칭만 보수적으로 인정하고, 그 밖의 지시어는 추측하지 않는다."""

    def test_disallow_typos_are_treated_as_disallow(self):
        for name in ("Disalow", "Dissalow", "Dissallow", "Diasllow", "Disallaw", "DISALOW", "dissallow", "  Dissallow  "):
            with self.subTest(name=name):
                robots = rm.parse("User-agent: *\n%s: /private\n" % name)
                self.assertFalse(robots.is_allowed(AGENT, "/private/x"))
                self.assertTrue(robots.is_allowed(AGENT, "/public"))
                self.assertEqual((robots.info.typo_alias_lines, robots.info.rule_lines, robots.info.ignored_lines), (1, 1, 0))

    def test_user_agent_typos_start_groups(self):
        for name in ("User agent", "Useragent", "USER AGENT", "UserAgent", "user   agent", "user\tagent"):
            with self.subTest(name=name):
                robots = rm.parse("%s: *\nDisallow: /p\n" % name)
                self.assertFalse(robots.is_allowed(AGENT, "/p"))
                self.assertEqual(robots.info.typo_alias_lines, 1)
                self.assertEqual(robots.info.user_agent_lines, 1)
        robots = rm.parse("User agent: muwa-source-fetch\nDisallow: /a\nUseragent: other\nDisallow: /b\n")
        self.assertFalse(robots.is_allowed(AGENT, "/a"))
        self.assertTrue(robots.is_allowed(AGENT, "/b"))

    def test_typo_and_canonical_forms_give_identical_decisions(self):
        paths = ("/", "/a", "/a/b", "/b", "/private/x", "/x.gif")
        canonical = rm.parse("User-agent: *\nDisallow: /a\nAllow: /a/b\nDisallow: *.gif$\n")
        typos = rm.parse("Useragent: *\nDisalow: /a\nAllow: /a/b\nDissallow: *.gif$\n")
        for path in paths:
            self.assertEqual(canonical.is_allowed(AGENT, path), typos.is_allowed(AGENT, path), path)
        self.assertEqual(typos.info.typo_alias_lines, 3)
        self.assertEqual(canonical.info.typo_alias_lines, 0)

    def test_other_names_are_not_guessed(self):
        for name in ("Disalloww", "Dissalowed", "Dis allow", "Disallow2", "Disallows", "Disallowed", "Alow", "Allo", "Allows", "Noindex",
                     "Host", "Clean-param", "Request-rate", "Disallow-all", "Dont-allow", "Deny", "Block", "Crawl-delays", "Dis-allow"):
            with self.subTest(name=name):
                robots = rm.parse("User-agent: *\n%s: /p\n" % name)
                self.assertTrue(robots.is_allowed(AGENT, "/p"))
                self.assertEqual((robots.info.ignored_lines, robots.info.typo_alias_lines, robots.info.rule_lines), (1, 0, 0))

    def test_unknown_user_agent_names_do_not_start_groups(self):
        for name in ("User-agents", "User_agent", "Useragents", "UA", "Agent", "User-agent-name"):
            with self.subTest(name=name):
                robots = rm.parse("%s: *\nDisallow: /p\n" % name)
                self.assertEqual(robots.groups, ())
                self.assertTrue(robots.is_allowed(AGENT, "/p"))
                self.assertEqual(robots.info.orphan_rule_lines, 1)
                self.assertEqual(robots.reliability, "no_groups")

    def test_allow_has_no_typo_aliases(self):
        for name in ("Alow", "Allw", "Alow"):
            robots = rm.parse("User-agent: *\nDisallow: /\n%s: /ok\n" % name)
            self.assertFalse(robots.is_allowed(AGENT, "/ok"))

    def test_typos_without_a_user_agent_do_not_count_as_a_group(self):
        robots = rm.parse("Disalow: /x\n")
        self.assertEqual((robots.groups, robots.info.orphan_rule_lines, robots.info.typo_alias_lines), ((), 1, 1))


class ReliabilitySignalTests(NoNetworkCase):
    """리뷰 M2: HTML·바이너리·의미 있는 지시어가 없는 응답을 호출자가 차단할 수 있어야 한다."""

    def code(self, data):
        return rm.parse(data).reliability

    def test_ok_and_counts(self):
        robots = rm.parse("# header\nUser-agent: *\nDisallow: /x\nAllow: /x/ok\nCrawl-delay: 3\nSitemap: https://example.org/s.xml\nNoindex: /z\nbroken line\n")
        info = robots.info
        self.assertEqual(robots.reliability, "ok")
        self.assertTrue(robots.usable)
        self.assertEqual((info.content_lines, info.recognized_lines, info.user_agent_lines, info.rule_lines, info.sitemap_lines,
                          info.crawl_delay_lines, info.ignored_lines, info.orphan_rule_lines, info.typo_alias_lines, info.invalid_utf8_lines),
                         (7, 5, 1, 2, 1, 1, 2, 0, 0, 0))
        self.assertFalse(info.binary)
        self.assertFalse(info.looks_like_html)
        self.assertEqual(info.bytes_len, len("# header\nUser-agent: *\nDisallow: /x\nAllow: /x/ok\nCrawl-delay: 3\nSitemap: https://example.org/s.xml\nNoindex: /z\nbroken line\n"))

    def test_legitimate_minimal_files_are_ok(self):
        for text in ("User-agent: *\nDisallow:\n", "User-agent: *\nAllow: /\n", "User-agent: a\nUser-agent: b\n", "Sitemap: https://example.org/s.xml\n",
                     "User-agent: *\nDisallow: /\n"):
            with self.subTest(text=text[:30]):
                self.assertEqual(self.code(text), "ok")

    def test_empty_comment_only_and_bom_only(self):
        for data in (b"", "", "   \n\t\n", "# only a comment\n# another", b"\xef\xbb\xbf", "\ufeff", "\r\n\r\n", b"\n# c\n"):
            with self.subTest(data=repr(data)[:20]):
                robots = rm.parse(data)
                self.assertEqual(robots.reliability, "empty")
                self.assertTrue(robots.usable)
                self.assertTrue(robots.is_allowed(AGENT, "/anything"))  # RFC: 규칙 없음 = 허용 (호출자가 "empty"를 구분할 수 있다)
                self.assertEqual(robots.info.content_lines, 0)

    def test_html_documents(self):
        for data in ("<!DOCTYPE html><html><head><title>404</title></head><body>Not found</body></html>", "<html></html>", "<HTML><BODY>x</BODY></HTML>",
                     "  \n\t<?xml version='1.0'?><error/>", "\ufeff<html>", b"<html>", "<!-- c --><p>x</p>", "<p>oops</p>",
                     "<html><body>\nUser-agent: *\nDisallow: /\n</body></html>", "User-agent: *\nDisallow: /x\n</html>",
                     "Not Found\n<body>x"):
            with self.subTest(data=repr(data)[:40]):
                robots = rm.parse(data)
                self.assertEqual(robots.reliability, "html")
                self.assertFalse(robots.usable)
                self.assertTrue(robots.info.looks_like_html)

    def test_binary_content(self):
        for data in (b"\x00\x01binary garbage \xff", b"User-agent: *\nDisallow: /x\n\x00", bytes(range(256)) * 4, "User-agent: *\nDisallow: /x\x00\n",
                     b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", b"\xff" * 200 + b"User-agent: *\n"):
            with self.subTest(data=repr(data)[:30]):
                robots = rm.parse(data)
                self.assertEqual(robots.reliability, "binary")
                self.assertFalse(robots.usable)
                self.assertTrue(robots.info.binary)

    def test_isolated_control_byte_is_not_binary(self):
        data = b"User-agent: *\nDisallow: /x\n#" + b"y" * 5000 + b"\x01\n"
        robots = rm.parse(data)
        self.assertFalse(robots.info.binary)
        self.assertEqual(robots.reliability, "ok")

    def test_a_single_nul_anywhere_is_binary_even_in_a_large_file(self):
        head = b"User-agent: *\nDisallow: /x\n#" + b"y" * 20000
        for data in (head + b"\x00\n", b"\x00" + head, head[:5000] + b"\x00" + head[5000:]):
            with self.subTest(size=len(data)):
                self.assertEqual(rm.parse(data).reliability, "binary")
        self.assertEqual(rm.parse(head.decode("ascii") + "\x00\n").reliability, "binary")  # str 입력도 동일

    def test_control_character_ratio_boundary(self):
        head = "User-agent: *\nDisallow: /x\n#"
        # (제어 문자 수, 전체 길이): 비율이 1% 이상이면 바이너리, 미만이면 일반 텍스트
        for controls, total in ((10, 200), (4, 300), (3, 300), (2, 300), (1, 1000)):
            text = head + "y" * (total - len(head) - controls) + "\x01" * controls
            self.assertEqual(len(text), total)
            expected = "binary" if controls / total >= 0.01 else "ok"
            with self.subTest(controls=controls, total=total, ratio=round(controls / total, 4)):
                self.assertEqual(rm.parse(text).reliability, expected)
                self.assertEqual(rm.parse(text.encode("ascii")).reliability, expected)
        # 정확히 1%: 100바이트 중 제어 바이트 1개
        exact = b"U" * 99 + b"\x01"
        self.assertEqual(len(exact), 100)
        self.assertEqual(rm.parse(exact).reliability, "binary")
        self.assertEqual(rm.parse(b"U" * 100 + b"\x01").reliability, "no_directives")  # 1/101 < 1%: 바이너리 아님 (인식된 지시어도 없음)
        self.assertEqual(rm.parse(b"U" * 99 + b"\x7f").reliability, "binary")  # DEL 도 제어 문자
        self.assertEqual(rm.parse("U" * 99 + "\x7f").reliability, "binary")
        self.assertEqual(rm.parse(b"U" * 100 + b"\x7f").reliability, "no_directives")

    def test_tab_cr_lf_are_not_control_noise(self):
        text = "User-agent: *\r\n\tDisallow:\t/x\r\n" * 200
        self.assertEqual(rm.parse(text).reliability, "ok")
        self.assertFalse(rm.parse(text).info.binary)

    def test_binary_takes_precedence_over_html_in_the_reliability_code(self):
        self.assertEqual(rm.parse(b"<html><body>\x00\x00</body></html>").reliability, "binary")
        robots = rm.parse(b"<html>\x00")
        self.assertTrue(robots.info.binary and robots.info.looks_like_html)
        self.assertEqual(robots.reliability, "binary")
        self.assertEqual(rm.parse(b"<html>").reliability, "html")

    def test_no_recognized_directives(self):
        for data in ("hello world\nthis is not a robots file", "Noindex: /\nHost: example.org\n", "Foo: bar", "Disallow2: /x", "Disalloww: /x\nAlow: /y",
                     "{\"error\": \"not found\"}", "404 page not found", "User-agent:\n", ":::\n::\n"):
            with self.subTest(data=repr(data)[:30]):
                robots = rm.parse(data)
                self.assertEqual(robots.reliability, "no_directives")
                self.assertFalse(robots.usable)
                self.assertEqual(robots.info.recognized_lines, 0)
                self.assertGreater(robots.info.ignored_lines, 0)
                self.assertTrue(robots.is_allowed(AGENT, "/x"))  # 신호를 무시하면 허용으로 보이므로 호출자가 차단해야 한다

    def test_rules_without_any_user_agent(self):
        for data in ("Disallow: /x\nAllow: /y\n", "Disallow: /\n", "Crawl-delay: 5\n", "Disalow: /x\n"):
            with self.subTest(data=repr(data)):
                robots = rm.parse(data)
                self.assertEqual(robots.reliability, "no_groups")
                self.assertFalse(robots.usable)
                self.assertEqual(robots.groups, ())

    def test_strict_mode_raises_the_reliability_code(self):
        cases = {"binary": b"\x00abc", "html": "<html></html>", "no_directives": "hello world", "no_groups": "Disallow: /x\n",
                 "invalid_utf8": b"User-agent: *\nDisallow: /caf\xe9/\n"}
        for code, data in cases.items():
            with self.subTest(code=code):
                self.assertEqual(rm.parse(data).reliability, code)
                with self.assertRaises(RobotsError) as cm:
                    rm.parse(data, strict=True)
                self.assertEqual(cm.exception.code, code)
        for data in ("User-agent: *\nDisallow: /x\n", "", "# nothing\n"):
            self.assertIs(rm.parse(data, strict=True).usable, True)

    def test_ensure_usable_returns_self_or_raises(self):
        robots = rm.parse("User-agent: *\nDisallow: /x\n")
        self.assertIs(robots.ensure_usable(), robots)
        with self.assertRaises(RobotsError):
            rm.parse("<html>").ensure_usable()

    def test_reliability_codes_are_documented_and_closed(self):
        seen = {self.code(d) for d in ("User-agent: *\n", "", b"\x00", "<html>", "x", "Disallow: /", b"User-agent: *\nDisallow: /\xff\n")}
        self.assertEqual(seen, set(rm.RELIABILITY_CODES))
        self.assertEqual(set(rm.USABLE_RELIABILITY), {"ok", "empty"})

    def test_info_is_not_part_of_equality(self):
        a = rm.parse("User-agent: *\nDisallow: /x\n")
        b = rm.parse("# comment\n\nUser-agent: *   # trailing\nDisallow: /x\nSitemap: https://example.org/s\n")
        self.assertEqual(a, b)
        self.assertNotEqual(a.info, b.info)
        self.assertEqual(rm.parse(b"User-agent: *\nDisallow: /x\n"), rm.parse("User-agent: *\nDisallow: /x\n"))

    def test_str_and_bytes_inputs_give_the_same_signals(self):
        for text in ("<html>", "User-agent: *\nDisallow: /x\n", "x y z", "# c"):
            self.assertEqual(rm.parse(text).reliability, rm.parse(text.encode("utf-8")).reliability)
            self.assertEqual(rm.parse(text).info, rm.parse(text.encode("utf-8")).info)

    def test_signals_do_not_change_matching(self):
        text = "Useragent: *\nDisalow: /x\nNoindex: /y\n"
        robots = rm.parse(text)
        self.assertEqual(robots.reliability, "ok")
        self.assertFalse(robots.is_allowed(AGENT, "/x"))
        self.assertTrue(robots.is_allowed(AGENT, "/y"))


class Utf8AndRawByteTests(NoNetworkCase):
    """리뷰 M3: UTF-8 오류가 허용 판정으로 이어지지 않는다 (원시 바이트 보존 + 신호)."""

    def test_latin1_byte_in_a_rule_is_preserved_and_still_blocks(self):
        robots = rm.parse(b"User-agent: *\nDisallow: /caf\xe9/\n")
        self.assertEqual(robots.groups[0].rules[0].pattern, "/caf%E9/")
        self.assertFalse(robots.is_allowed(AGENT, "/caf%E9/x"))
        self.assertFalse(robots.is_allowed(AGENT, "/caf%e9/x"))
        self.assertTrue(robots.is_allowed(AGENT, "/cafe/x"))
        self.assertEqual(robots.info.invalid_utf8_lines, 1)
        self.assertEqual(robots.reliability, "invalid_utf8")
        self.assertFalse(robots.usable)

    def test_no_replacement_character_is_ever_stored(self):
        for raw in (b"\xe9", b"\xff\xfe", b"\xc0\xaf", b"\xe3\x83", b"\xed\xa0\x80", b"\xf8\x88\x80\x80\x80"):
            robots = rm.parse(b"User-agent: *\nDisallow: /a" + raw + b"b\n")
            pattern = robots.groups[0].rules[0].pattern
            self.assertNotIn("%EF%BF%BD", pattern, raw)
            self.assertTrue(pattern.isascii(), raw)
            self.assertEqual(robots.info.invalid_utf8_lines, 1)

    def test_raw_bytes_map_one_to_one_to_percent_escapes(self):
        self.assertEqual(rm.parse(b"User-agent: *\nDisallow: /\xc0\xaf\n").groups[0].rules[0].pattern, "/%C0%AF")
        self.assertEqual(rm.parse(b"User-agent: *\nDisallow: /\xe3\x83\n").groups[0].rules[0].pattern, "/%E3%83")
        self.assertEqual(rm.parse(b"User-agent: *\nDisallow: /\xed\x95\x9c\xff\n").groups[0].rules[0].pattern, "/%ED%95%9C%FF")
        # 유효한 UTF-8 은 그대로 정상 인코딩
        self.assertEqual(rm.parse("User-agent: *\nDisallow: /café/\n".encode("utf-8")).groups[0].rules[0].pattern, "/caf%C3%A9/")

    def test_valid_utf8_rules_match_both_spellings_and_raise_no_signal(self):
        robots = rm.parse("User-agent: *\nDisallow: /café/\n".encode("utf-8"))
        self.assertFalse(robots.is_allowed(AGENT, "/caf%C3%A9/x"))
        self.assertFalse(robots.is_allowed(AGENT, "/café/x"))
        self.assertEqual(robots.info.invalid_utf8_lines, 0)
        self.assertEqual(robots.reliability, "ok")

    def test_invalid_bytes_inside_comments_are_ignored(self):
        robots = rm.parse(b"User-agent: *   # caf\xe9 \xff\n# \xc0\xaf\nDisallow: /x\n")
        self.assertEqual(robots.info.invalid_utf8_lines, 0)
        self.assertEqual(robots.reliability, "ok")
        self.assertFalse(robots.is_allowed(AGENT, "/x"))

    def test_invalid_bytes_in_any_content_line_are_counted(self):
        data = b"User-agent: bot\xff\nDisallow: /x\nSitemap: https://example.org/caf\xe9.xml\nUnknown: \xfe\n"
        robots = rm.parse(data)
        self.assertEqual(robots.info.invalid_utf8_lines, 3)
        self.assertEqual(robots.reliability, "invalid_utf8")
        with self.assertRaises(RobotsError) as cm:
            rm.parse(data, strict=True)
        self.assertEqual(cm.exception.code, "invalid_utf8")

    def test_high_invalid_byte_ratio_is_binary(self):
        robots = rm.parse(b"\xff" * 120 + b"\nUser-agent: *\nDisallow: /x\n")
        self.assertEqual(robots.reliability, "binary")

    def test_str_input_with_surrogates(self):
        robots = rm.parse("User-agent: *\nDisallow: /a\udc80b\n")  # surrogateescape 로 보존된 원시 바이트 0x80
        self.assertEqual(robots.groups[0].rules[0].pattern, "/a%80b")
        self.assertEqual(robots.info.invalid_utf8_lines, 1)
        lone = rm.parse("User-agent: *\nDisallow: /a\ud800b\n")
        self.assertEqual(lone.groups[0].rules[0].pattern, "/a%ED%A0%80b")
        self.assertEqual(lone.reliability, "invalid_utf8")

    def test_bytes_and_str_agree_for_valid_utf8(self):
        text = "User-agent: *\nDisallow: /한글/\nAllow: /한글/ok\n"
        self.assertEqual(rm.parse(text), rm.parse(text.encode("utf-8")))
        self.assertEqual(rm.parse(text).info, rm.parse(text.encode("utf-8")).info)

    def test_utf8_bom_bytes_are_not_an_error(self):
        robots = rm.parse(b"\xef\xbb\xbfUser-agent: *\nDisallow: /x\n")
        self.assertEqual(robots.reliability, "ok")
        self.assertEqual(robots.info.invalid_utf8_lines, 0)


class NormalizationBoundaryTests(NoNetworkCase):
    """리뷰 M3: 공백·퍼센트 인코딩 경계."""

    def blocks(self, pattern, target):
        return not rm.parse("User-agent: *\nDisallow: %s\n" % pattern).is_allowed(AGENT, target)

    def test_spaces_and_controls_in_rule_values_match_encoded_targets(self):
        self.assertEqual(rm.parse("User-agent: *\nDisallow: /a b\n").groups[0].rules[0].pattern, "/a%20b")
        self.assertTrue(self.blocks("/a b", "/a%20b/x"))
        self.assertTrue(self.blocks("/a b", "/a%20b"))
        self.assertFalse(self.blocks("/a b", "/ab"))
        self.assertTrue(self.blocks("/my folder/private", "/my%20folder/private/x"))
        self.assertEqual(rm.parse("User-agent: *\nDisallow: /a\tb\n").groups[0].rules[0].pattern, "/a%09b")
        self.assertTrue(self.blocks("/a\tb", "/a%09b"))
        self.assertTrue(self.blocks("/a%20b", "/a%20b"))
        self.assertTrue(self.blocks("/a%20b", "/a%20B".replace("B", "b")))

    def test_targets_with_raw_whitespace_or_controls_are_still_rejected(self):
        robots = rm.parse("User-agent: *\nDisallow: /a b\n")
        for target in ("/a b", "/a\tb", "/a\nb", "/a\x00b", "/a\\b"):
            with self.subTest(target=ascii(target)):
                self.assertEqual(self.error(robots, target), "bad_target")

    def error(self, robots, target):
        with self.assertRaises(RobotsError) as cm:
            robots.is_allowed(AGENT, target)
        return cm.exception.code

    def test_unsafe_ascii_characters_are_encoded_on_both_sides(self):
        for ch in '"<>^`{|}':
            code = "%%%02X" % ord(ch)
            with self.subTest(ch=ch):
                self.assertTrue(self.blocks("/x%sy" % ch, "/x%sy" % ch))
                self.assertTrue(self.blocks("/x%sy" % ch, "/x%sy" % code))
                self.assertTrue(self.blocks("/x%sy" % ch, "/x%sy" % code.lower()))
                self.assertTrue(self.blocks("/x%sy" % code, "/x%sy" % ch))
                self.assertFalse(self.blocks("/x%sy" % ch, "/xy"))

    def test_reserved_characters_are_not_equivalent_to_their_escapes(self):
        for ch in ":@+=!'(),;&":
            code = "%%%02X" % ord(ch)
            with self.subTest(ch=ch):
                self.assertTrue(self.blocks("/a%sb" % ch, "/a%sb/x" % ch))
                self.assertFalse(self.blocks("/a%sb" % ch, "/a%sb/x" % code))
                self.assertFalse(self.blocks("/a%sb" % code, "/a%sb/x" % ch))
                self.assertTrue(self.blocks("/a%sb" % code, "/a%sb/x" % code.lower()))

    def test_unreserved_escapes_equal_literals_for_every_unreserved_character(self):
        for ch in "AZaz09-._~":
            code = "%%%02x" % ord(ch)
            with self.subTest(ch=ch):
                self.assertTrue(self.blocks("/p%sq" % ch, "/p%sq" % code))
                self.assertTrue(self.blocks("/p%sq" % code, "/p%sq" % ch))
                self.assertTrue(self.blocks("/p%sq" % code.upper(), "/p%sq" % ch))

    def test_percent_edge_cases(self):
        cases = (("%", "%25"), ("100%", "100%25"), ("%2", "%252"), ("%g1", "%25g1"), ("%4z", "%254z"), ("%%41", "%25A"), ("%41", "A"), ("%4", "%254"),
                 ("a%", "a%25"), ("%41%", "A%25"), ("%2f", "%2F"), ("%2F", "%2F"), ("%7e", "~"), ("%7E", "~"), ("%7b", "%7B"), ("%0a", "%0A"),
                 ("%25", "%25"), ("%2525", "%2525"), ("%e9", "%E9"))
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(rm.normalize_path(raw), expected)

    def test_encoded_wildcard_and_anchor_characters_are_literal(self):
        self.assertTrue(self.blocks("/a*b", "/aXb"))
        self.assertFalse(self.blocks("/a%2Ab", "/aXb"))
        self.assertTrue(self.blocks("/a%2Ab", "/a%2Ab"))
        self.assertTrue(self.blocks("/a%2ab", "/a%2Ab"))
        self.assertFalse(self.blocks("/a%2Ab", "/a*b"))  # 날것의 '*'와 '%2A'는 같지 않다
        self.assertFalse(self.blocks("/a%24", "/a"))
        self.assertTrue(self.blocks("/a%24", "/a%24"))
        self.assertTrue(self.blocks("/a$", "/a"))
        self.assertFalse(self.blocks("/a$", "/a%24"))

    def test_normalization_is_idempotent(self):
        for raw in ("/a b", "/%7e/%2F/é/%zz/%", "/x|y{z}", "/한글?q=%41&r=%20", "/%25%32%35"):
            once = rm.normalize_path(raw)
            self.assertEqual(rm.normalize_path(once), once, raw)


class ImplicitRobotsTxtTests(NoNetworkCase):
    """리뷰 L1: RFC 9309 §2.2.2 — /robots.txt URI는 항상 허용."""

    def test_always_allowed_whatever_the_rules(self):
        for rules in ("Disallow: /", "Disallow: /robots.txt", "Disallow: /*", "Disallow: /*.txt$", "Disallow: /r", "Disallow: /robots.txt$",
                      "Disallow: /*\nDisallow: /robots.txt\nDisallow: /robots", "Allow: /x\nDisallow: /"):
            robots = rm.parse("User-agent: *\n%s\n" % rules)
            with self.subTest(rules=rules):
                decision = robots.evaluate(AGENT, "/robots.txt")
                self.assertTrue(decision.allowed)
                self.assertEqual(decision.reason, "implicit_robots_txt")
                self.assertIsNone(decision.rule)
                self.assertTrue(robots.is_allowed(AGENT, "https://example.org/robots.txt"))
                self.assertTrue(robots.is_allowed(AGENT, "http://example.org/robots.txt#frag"))
                self.assertTrue(robots.is_allowed(AGENT, "/robots.txt#frag"))

    def test_percent_encoded_spelling_of_the_same_uri_is_allowed(self):
        robots = rm.parse("User-agent: *\nDisallow: /\n")
        self.assertTrue(robots.is_allowed(AGENT, "/%72obots.txt"))
        self.assertTrue(robots.is_allowed(AGENT, "/robots%2Etxt"))

    def test_other_paths_follow_the_normal_rules(self):
        robots = rm.parse("User-agent: *\nDisallow: /\n")
        for target in ("/robots.txt?x=1", "/robots.txt/", "/ROBOTS.TXT", "/robots.txt2", "/a/robots.txt", "/robots", "/robots.TXT", "/robot.txt"):
            with self.subTest(target=target):
                self.assertFalse(robots.is_allowed(AGENT, target))
                self.assertEqual(robots.evaluate(AGENT, target).reason, "")

    def test_applies_to_every_group_kind(self):
        for text, agent, kind in (("User-agent: muwa-source-fetch\nDisallow: /\n", AGENT, "specific"), ("User-agent: *\nDisallow: /\n", AGENT, "wildcard"),
                                  ("User-agent: other\nDisallow: /\n", AGENT, "none"), ("", AGENT, "none")):
            decision = rm.parse(text).evaluate(agent, "/robots.txt")
            self.assertTrue(decision.allowed)
            self.assertEqual(decision.group, kind)

    def test_normal_allow_decisions_have_no_reason(self):
        decision = rm.parse("User-agent: *\nDisallow: /x\n").evaluate(AGENT, "/public")
        self.assertEqual((decision.allowed, decision.reason), (True, ""))


class TargetValidationTests(NoNetworkCase):
    """리뷰 L2: 비정상 대상은 허용으로 오판하지 않고 거부한다."""

    ROBOTS = "User-agent: *\nDisallow: /private\n"

    def code(self, target):
        with self.assertRaises(RobotsError) as cm:
            rm.parse(self.ROBOTS).is_allowed(AGENT, target)
        return cm.exception.code

    def test_scheme_relative_and_backslash_targets_are_rejected(self):
        for target in ("//example.org/private/x", "//", "///private", "//example.org", "/\\private", "\\private", "/a\\b", "/private\\x"):
            with self.subTest(target=target):
                self.assertEqual(self.code(target), "bad_target")

    def test_malformed_absolute_urls_are_rejected(self):
        for target in ("https:///private", "http:///x", "https://", "http://", "https://user@example.org/private", "https://user:pw@example.org/x",
                       "https://@example.org/x", "HTTPS://example.org/private", "ftp://example.org/private", "file:///private", "javascript:alert(1)",
                       "example.org/private", "private", "?x=1", "#frag", "", None, 5, b"/private", ["/private"]):
            with self.subTest(target=repr(target)[:30]):
                self.assertEqual(self.code(target), "bad_target")

    def test_control_characters_whitespace_and_length(self):
        for target in ("/a b", "/a\tb", "/a\nb", "/a\rb", "/a\x00b", "/a\x7fb", " /private", "/private ", "/" + "x" * rm.MAX_TARGET_LEN):
            with self.subTest(target=ascii(target)[:30]):
                self.assertEqual(self.code(target), "bad_target")
        self.assertTrue(rm.parse(self.ROBOTS).is_allowed(AGENT, "/" + "x" * (rm.MAX_TARGET_LEN - 1)))

    def test_valid_targets_still_work(self):
        robots = rm.parse(self.ROBOTS)
        for target, allowed in (("/", True), ("/a", True), ("/private", False), ("/private/x?y=1", False), ("https://example.org", True),
                                ("https://example.org/private", False), ("http://example.org/private?x=1", False), ("https://example.org:8443/private", False),
                                ("https://example.org/private#frag", False), ("/é", True), ("/%ED%95%9C", True)):
            with self.subTest(target=target):
                self.assertEqual(robots.is_allowed(AGENT, target), allowed)

    def test_rejection_is_an_exception_not_an_allow(self):
        robots = rm.parse(self.ROBOTS)
        with self.assertRaises(RobotsError):
            robots.evaluate(AGENT, "//example.org/private/x")
        with self.assertRaises(RobotsError):
            robots.crawl_delay("*")


class DocumentationTests(unittest.TestCase):
    """RFC 요구와 당사 보수적 정책이 구분되어 문서화되어 있다."""

    def test_module_docstring_separates_rfc_requirements_from_policy(self):
        doc = rm.__doc__
        for marker in ("[RFC]", "[정책]", "§2.1", "§2.2.1", "§2.2.2", "§2.2.3", "§2.2.4", "§2.5", "UNVERIFIED", "RobotsError"):
            self.assertIn(marker, doc)
        for topic in ("/robots.txt", "오타", "disalow", "useragent", "512 KiB", "원시 바이트", "reliability", "Crawl-delay", "SafeFetcher", "invalid_utf8"):
            self.assertIn(topic, doc, topic)

    def test_documented_limits_match_the_constants(self):
        self.assertEqual(rm.MAX_BYTES, 512 * 1024)
        self.assertEqual(rm.MAX_PATTERN_LEN, 4096)
        self.assertEqual(rm.MAX_CRAWL_DELAY, 86400.0)
        self.assertGreaterEqual(rm.MAX_BYTES, 500 * 1024)  # RFC 9309 §2.5: 최소 500 KiB


class TestFixtures:
    SAMPLE = ("User-agent: *\nDisallow: /private/\nAllow: /private/public/\nCrawl-delay: 3\n\nUser-agent: MUWA-source-fetch\n"
              "Disallow: /tmp\nSitemap: https://example.org/sitemap.xml\n")


class StaticTests(unittest.TestCase):
    def test_standard_library_only_no_network_modules(self):
        tree = ast.parse((ROOT / "robots_matcher.py").read_text(encoding="utf-8"))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module or "")
        self.assertEqual(imports - {"__future__", "math", "re", "dataclasses", "urllib.parse"}, set())
        for forbidden in ("socket", "ssl", "http", "urllib.request", "subprocess", "requests", "ops_state", "backends"):
            self.assertNotIn(forbidden, imports)


if __name__ == "__main__":
    unittest.main()

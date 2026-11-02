"""P3 통합 테스트: L1 shadow 검증, 라이프스타일 카테고리 독립 중복 판정, 키 비노출, 로그 비추적.

AI 호출·네트워크 없음: 수집 함수와 URL 검사는 가짜로 대체한다.
"""

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import content_quality as cq
import run_lifestyle
from dates import WeekPeriod

ROOT = Path(__file__).resolve().parent.parent
W3 = WeekPeriod(year=2026, week_label="2026-09-W3", start=date(2026, 9, 21), end=date(2026, 9, 27))
FORBIDDEN = ("ak1:", "tk1:", "akey", "article_key")


def it(title, url, d="2026-09-23", source="매체"):
    return {"title": title, "summary": "요약", "source": source, "source_url": url, "date": d}


def cat(domestic=(), global_=()):
    return {"range_label": "r", "domestic": list(domestic), "global": list(global_)}


def write_week(base, name, payload):
    path = base / "data" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class ShadowTests(unittest.TestCase):
    def _index_with(self, url, title="완전히 다른 제목의 기존 기사"):
        return [cq.make_index_entry(title, url, "2026-09-22")]

    def test_l1_only_match_is_logged_but_item_is_kept(self):
        index = self._index_with("https://example.com/news/1")
        log = []
        kept = cq.filter_items([it("새로 들어온 전혀 다른 기사 제목", "https://example.com:443/news/1")],
                               W3, index, "architecture", "domestic", log, verify_urls=False)
        self.assertEqual(len(kept), 1)  # 판정에는 영향 없음
        shadow = [r for r in log if r.get("action") == "shadow"]
        self.assertEqual(len(shadow), 1)
        self.assertEqual(shadow[0]["reason"], "l1_only_match")
        self.assertTrue(shadow[0]["key"].startswith("ak1:"))
        self.assertEqual(cq.count_shadow(log), 1)
        self.assertNotIn("http", json.dumps(shadow[0]))  # URL 전문을 기록하지 않는다

    def test_l0_duplicate_is_excluded_and_not_shadowed(self):
        index = self._index_with("https://example.com/news/1")
        log = []
        kept = cq.filter_items([it("새로 들어온 전혀 다른 기사 제목", "https://www.example.com/news/1/?utm_source=x")],
                               W3, index, "architecture", "domestic", log, verify_urls=False)
        self.assertEqual(kept, [])
        self.assertEqual(log[0]["reason"], "duplicate_url")
        self.assertEqual(cq.count_shadow(log), 0)

    def test_find_duplicate_still_uses_l0_only(self):
        index = self._index_with("https://example.com/news/1")
        self.assertIsNone(cq.find_duplicate(index, "새로 들어온 전혀 다른 기사 제목",
                                            "https://example.com:443/news/1", "2026-09-23"))

    def test_shadow_does_not_change_decisions(self):
        def run(patch_shadow):
            index = self._index_with("https://example.com/news/1")
            index.append(cq.make_index_entry("이미 있는 기사 제목 하나", "https://b.com/x", "2026-09-22"))
            items = [it("새로 들어온 전혀 다른 기사 제목", "https://example.com:443/news/1"),
                     it("이미 있는 기사 제목 하나", "https://c.com/y"),
                     it("또 다른 신규 기사 제목 입니다", "https://d.com/z")]
            log = []
            ctx = mock.patch.object(cq, "_shadow_l1", lambda *a, **k: None) if patch_shadow else contextlib.nullcontext()
            with ctx:
                kept = cq.filter_items(items, W3, index, "culture", "global", log, verify_urls=False)
            return [k["title"] for k in kept], [(r["title"], r["reason"]) for r in log if r["action"] == "excluded"]

        self.assertEqual(run(False), run(True))

    def test_kept_items_gain_no_extra_fields(self):
        item = it("새로 들어온 전혀 다른 기사 제목", "https://example.com:443/news/1")
        kept = cq.filter_items([item], W3, self._index_with("https://example.com/news/1"), "culture", "global", [],
                               verify_urls=False)
        self.assertEqual(set(kept[0]), set(item))

    def test_index_entries_carry_akey_internally(self):
        entry = cq.make_index_entry("충분히 긴 테스트 기사 제목", "https://a.com/x", "2026-09-23")
        self.assertTrue(entry["akey"].startswith("ak1:"))


class SectionIndexTests(unittest.TestCase):
    def test_same_week_url_dedup_flag(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            write_week(base, "2026-09-W3_culture.json", {"domestic": [it("기존 기사 제목입니다 하나", "https://a.com/x")], "global": []})
            write_week(base, "2026-09-W2_culture.json", {"domestic": [it("지난주 기사 제목입니다", "https://a.com/old")], "global": []})
            default = cq.load_section_index(base / "data", "culture", "2026-09-W3")
            strict = cq.load_section_index(base / "data", "culture", "2026-09-W3", same_week_url_dedup=True)
            by = lambda idx: {e["title"]: e["url_dedup"] for e in idx}
            self.assertEqual(by(default), {"기존 기사 제목입니다 하나": False, "지난주 기사 제목입니다": True})
            self.assertEqual(by(strict), {"기존 기사 제목입니다 하나": True, "지난주 기사 제목입니다": True})

    def test_other_categories_and_sections_are_not_read(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            write_week(base, "2026-09-W2_lifestyle_trend.json", {"domestic": [it("트렌드 카테고리 기사 제목 하나", "https://a.com/1")], "global": []})
            write_week(base, "2026-09-W2_trend.json", {"domestic": [it("주거 트렌드 기사 제목 하나", "https://a.com/2")], "global": []})
            self.assertEqual(cq.load_section_index(base / "data", "trend", "2026-09-W3"),
                             cq.load_section_index(base / "data", "trend", "2026-09-W3"))
            titles = {e["title"] for e in cq.load_section_index(base / "data", "trend", "2026-09-W3")}
            self.assertEqual(titles, {"주거 트렌드 기사 제목 하나"})  # lifestyle_trend 파일은 섞이지 않는다


class LifestyleRunTests(unittest.TestCase):
    def _run(self, base, categories):
        real_filter = cq.filter_items
        stack = contextlib.ExitStack()
        with stack:
            for name, value in (("DATA_DIR", base / "data"), ("INDEX_PATH", base / "data" / "insight_index.json"),
                                ("BASE_DIR", base)):
                stack.enter_context(mock.patch.object(run_lifestyle, name, value))
            stack.enter_context(mock.patch.object(run_lifestyle, "get_last_completed_week_period", lambda: W3))
            stack.enter_context(mock.patch.object(run_lifestyle, "prepare_backend", lambda log=print: (object(), None)))
            stack.enter_context(mock.patch.object(
                run_lifestyle, "collect_lifestyle", lambda period, failures, *, backend: categories))
            stack.enter_context(mock.patch.object(
                run_lifestyle, "filter_items", lambda *a, **k: real_filter(*a, fetch=lambda u: 200, **k)))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_lifestyle.main(), 0)

    def _log(self, base):
        return json.loads((base / "logs" / "2026-09-W3_collect_log.json").read_text(encoding="utf-8"))["runs"]

    def _items(self, base, category):
        path = base / "data" / f"2026-09-W3_{category}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return [x["title"] for x in data["domestic"] + data["global"]]

    def test_same_article_allowed_across_categories_and_housing_sections(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            write_week(base, "2026-09-W2_trend.json", {"domestic": [it("주거 섹션에 있는 같은 기사 제목", "https://shared.com/a")], "global": []})
            write_week(base, "2026-09-W3_culture.json", {"domestic": [it("문화 카테고리 기존 기사 제목", "https://shared.com/b")], "global": []})
            cats = {
                "architecture": cat([it("주거 섹션에 있는 같은 기사 제목", "https://shared.com/a"),
                                     it("문화 카테고리 기존 기사 제목", "https://shared.com/b")]),
                "culture": cat([it("문화 카테고리 기존 기사 제목", "https://shared.com/b")]),  # 같은 카테고리 재등장
            }
            self._run(base, cats)
            self.assertEqual(len(self._items(base, "architecture")), 2)  # 주거·다른 카테고리와 중복 허용
            self.assertEqual(self._items(base, "culture"), ["문화 카테고리 기존 기사 제목"])  # 자기 카테고리 중복은 차단
            run = self._log(base)[0]
            multi = [r for r in run["items"] if r["action"] == "multi_classified"]
            self.assertEqual({(r["category"], tuple(r["also_in"])) for r in multi},
                             {("architecture", ("trend",)), ("architecture", ("culture",))})
            self.assertTrue(any(r.get("reason") == "duplicate_url" and r["category"] == "culture" for r in run["items"]))
            self.assertNotIn("http", json.dumps(multi))  # 로그에 URL 전문을 남기지 않는다
            self.assertIn("shadow_l1_only_matches", run)

    def test_new_items_do_not_leak_between_categories_in_one_run(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            same = it("두 카테고리에 모두 들어갈 기사 제목", "https://shared.com/x")
            self._run(base, {"architecture": cat([same]), "culture": cat([same])})
            self.assertEqual(self._items(base, "architecture"), [same["title"]])
            self.assertEqual(self._items(base, "culture"), [same["title"]])
            multi = [r for r in self._log(base)[0]["items"] if r["action"] == "multi_classified"]
            self.assertEqual({r["category"] for r in multi}, {"architecture", "culture"})  # 양쪽 모두 기록(순서 무관)

    def test_category_order_does_not_change_results(self):
        a = it("첫째 카테고리 기사 제목 입니다", "https://shared.com/1")
        b = it("둘째 카테고리 기사 제목 입니다", "https://shared.com/2")
        results = []
        for order in (("architecture", "culture"), ("culture", "architecture")):
            with tempfile.TemporaryDirectory() as t:
                base = Path(t)
                cats = {k: cat([a, b]) for k in order}
                self._run(base, cats)
                results.append({k: self._items(base, k) for k in ("architecture", "culture")})
        self.assertEqual(results[0], results[1])

    def test_rerun_is_idempotent_within_a_category(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            cats = {"architecture": cat([it("재실행 확인용 기사 제목 입니다", "https://a.com/r")])}
            self._run(base, cats)
            path = base / "data" / "2026-09-W3_architecture.json"
            first = path.read_text(encoding="utf-8")
            self._run(base, cats)
            self.assertEqual(path.read_text(encoding="utf-8"), first)
            runs = self._log(base)
            self.assertIn("duplicate_url", [r.get("reason") for r in runs[1]["items"]])

    def test_previous_weeks_of_same_category_still_block(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            write_week(base, "2026-09-W2_architecture.json", {"domestic": [it("지난주에 이미 수집한 기사 제목", "https://a.com/old", "2026-09-15")], "global": []})
            self._run(base, {"architecture": cat([it("지난주에 이미 수집한 기사 제목", "https://a.com/old")])})
            self.assertEqual(self._items(base, "architecture"), [])  # 이전 주 동일 카테고리 기사와 중복 -> 신규 0건
            self.assertIn("duplicate_url", [r.get("reason") for r in self._log(base)[0]["items"]])


class NoKeyExposureTests(unittest.TestCase):
    def _assert_clean(self, text, where):
        for token in FORBIDDEN:
            self.assertNotIn(token, text, f"{token} leaked into {where}")

    def test_generated_json_files_contain_no_keys(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            same = it("두 카테고리에 모두 들어갈 기사 제목", "https://shared.com:443/x")
            tc = LifestyleRunTests("test_new_items_do_not_leak_between_categories_in_one_run")
            tc._run(base, {"architecture": cat([same]), "culture": cat([same])})
            files = list((base / "data").glob("*.json"))
            self.assertTrue(files)
            for path in files:
                self._assert_clean(path.read_text(encoding="utf-8"), path.name)

    def test_tracked_data_directory_contains_no_keys(self):
        for path in sorted((ROOT / "data").glob("*.json")):
            self._assert_clean(path.read_text(encoding="utf-8"), path.name)

    def test_site_and_api_sources_do_not_reference_keys(self):
        for rel in ("index.html", "api"):
            target = ROOT / rel
            files = [target] if target.is_file() else sorted(target.rglob("*.js"))
            for path in files:
                self._assert_clean(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT)))


@unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "git checkout required")
class LogsAreUntrackedTests(unittest.TestCase):
    def _git(self, *args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")

    def test_logs_directory_is_ignored_and_untracked(self):
        self.assertEqual(self._git("check-ignore", "-q", "logs/2026-09-W3_collect_log.json").returncode, 0)
        self.assertEqual(self._git("ls-files", "logs").stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()

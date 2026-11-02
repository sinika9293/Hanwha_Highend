import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import run_lifestyle
from content_quality import (
    check_url,
    filter_items,
    load_existing_index,
    make_index_entry,
    merge_category_file,
    normalize_title,
    normalize_url,
    parse_date,
)
from dates import WeekPeriod

PERIOD = WeekPeriod(year=2026, week_label="2026-09-W3", start=date(2026, 9, 21), end=date(2026, 9, 27))


def item(title="가나다 라마바 사아자", url="https://a.com/x", d="2026-09-23"):
    return {"title": title, "summary": "s", "source": "m", "source_url": url, "date": d}


def run(items, index=None, fetch=lambda u: 200):
    log = []
    kept = filter_items(items, PERIOD, [] if index is None else index, "k", "domestic", log, fetch=fetch)
    return kept, log


class DateFilterTests(unittest.TestCase):
    def test_boundaries(self):
        kept, log = run([item(d="2026-09-21", title="aaa1", url="https://a.com/1"),
                         item(d="2026-09-27", title="bbb2", url="https://a.com/2"),
                         item(d="2026-09-20", title="ccc3", url="https://a.com/3"),
                         item(d="2026-09-28", title="ddd4", url="https://a.com/4")])
        self.assertEqual([i["title"] for i in kept], ["aaa1", "bbb2"])
        self.assertEqual([l["reason"] for l in log if l["action"] == "excluded"], ["date_out_of_range"] * 2)

    def test_missing_and_unparseable(self):
        kept, log = run([item(d=""), item(d="어제", title="zzz9", url="https://a.com/9")])
        self.assertEqual(kept, [])
        self.assertEqual([l["reason"] for l in log], ["date_missing", "date_unparseable"])

    def test_parse_formats(self):
        self.assertEqual(parse_date("2026.9.23"), date(2026, 9, 23))
        self.assertIsNone(parse_date("2026-13-40"))


class DedupTests(unittest.TestCase):
    def test_url_normalization(self):
        self.assertEqual(
            normalize_url("HTTPS://www.A.com/x/?utm_source=z&id=1#top"), normalize_url("https://a.com/x?id=1")
        )

    def test_duplicate_url_with_tracking(self):
        idx = [make_index_entry("기존 기사", "https://a.com/x", "2026-09-22")]
        kept, log = run([item(title="전혀 다른 제목입니다", url="https://www.a.com/x/?utm_medium=q")], idx)
        self.assertEqual(kept, [])
        self.assertEqual(log[0]["reason"], "duplicate_url")

    def test_duplicate_title_exact(self):
        idx = [make_index_entry("롯데 하이엔드, 신규 단지!", "https://a.com/1", "2026-09-22")]
        kept, log = run([item(title="롯데 하이엔드 신규 단지", url="https://b.com/2")], idx)
        self.assertEqual(kept, [])
        self.assertEqual(log[0]["reason"], "duplicate_title")

    def test_syndicated_republish(self):
        idx = [make_index_entry("삼성물산 성수3지구 재개발 수주 포스터 협업", "https://a.com/1", "2026-09-22")]
        kept, log = run([item(title="삼성물산 성수3지구 재개발 수주, 포스터 협업", url="https://b.com/2", d="2026-09-23")], idx)
        self.assertEqual(kept, [])
        self.assertEqual(log[0]["reason"], "duplicate_title")  # 구두점 차이는 정규화로 동일
        kept, log = run([item(title="삼성물산 성수3지구 재개발 수주 포스터 협업안", url="https://b.com/2", d="2026-09-23")], idx)
        self.assertEqual(log[0]["reason"], "duplicate_syndicated")

    def test_similar_only_is_kept_and_logged(self):
        idx = [make_index_entry("한남4구역 삼성물산 한강뷰 설계안 공개", "https://a.com/1", "2026-09-01")]
        kept, log = run([item(title="한남4구역 삼성물산 한강뷰 설계안 전격 공개", url="https://b.com/2", d="2026-09-23")], idx)
        self.assertEqual(len(kept), 1)  # 유사도 0.94지만 날짜가 멀어 재배포로 단정하지 않는다
        self.assertTrue(any(l["reason"] == "similar_title_kept" for l in log))
        kept, log = run([item(title="한남4구역 삼성물산 한강뷰 극대화 설계 적용 UN스튜디오", url="https://b.com/3")], idx)
        self.assertEqual(len(kept), 1)  # 유사도가 낮으면 로그도 없이 통과
        self.assertEqual(log, [])

    def test_syndication_not_applied_when_dates_far(self):
        idx = [make_index_entry("삼성물산 성수3지구 재개발 수주 포스터 협업", "https://a.com/1", "2026-09-01")]
        kept, _ = run([item(title="삼성물산 성수3지구 재개발 수주 포스터 협업안", url="https://b.com/2", d="2026-09-23")], idx)
        self.assertEqual(len(kept), 1)

    def test_duplicate_within_same_run(self):
        kept, _ = run([item(), item()])
        self.assertEqual(len(kept), 1)

    def test_normalize_title(self):
        self.assertEqual(normalize_title(" A-B  c! "), "abc")


class UrlCheckTests(unittest.TestCase):
    def test_statuses(self):
        self.assertEqual(check_url("https://a.com", lambda u: 200)[0], "ok")
        self.assertEqual(check_url("https://a.com", lambda u: 404), ("invalid", "http_404"))
        self.assertEqual(check_url("https://a.com", lambda u: 410)[0], "invalid")
        self.assertEqual(check_url("not a url")[0], "invalid")
        for code in (403, 500, 503, 429):
            self.assertEqual(check_url("https://a.com", lambda u, c=code: c)[0], "unverified")

    def test_network_error_is_unverified(self):
        def boom(u):
            raise TimeoutError()

        self.assertEqual(check_url("https://a.com", boom), ("unverified", "TimeoutError"))

    def test_unverified_keeps_url_invalid_blanks_new_item(self):
        kept, log = run([item()], fetch=lambda u: 403)
        self.assertEqual(kept[0]["source_url"], "https://a.com/x")
        self.assertEqual(log[0]["url_status"], "unverified")
        kept, log = run([item()], fetch=lambda u: 404)
        self.assertEqual(kept[0]["source_url"], "")
        self.assertEqual(log[0]["url_status"], "invalid")

    def test_empty_url_not_checked(self):
        calls = []
        kept, _ = run([item(url="")], fetch=lambda u: calls.append(u) or 200)
        self.assertEqual(calls, [])
        self.assertEqual(len(kept), 1)


class MergeTests(unittest.TestCase):
    def test_existing_preserved_and_rerun_idempotent(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "2026-09-W3_x.json"
            old = item(title="기존 항목 하나", url="https://a.com/old")
            old["image_url"] = "https://img/1"
            path.write_text(json.dumps({"week": "w", "category": "x", "range_label": "r",
                                        "domestic": [old], "global": []}, ensure_ascii=False), encoding="utf-8")
            before = path.read_text(encoding="utf-8")

            incoming = [item(title="기존 항목 하나", url="https://a.com/old"),
                        item(title="새 항목 둘", url="https://a.com/new")]
            index = load_existing_index(Path(t))
            kept, _ = run(incoming, index)
            merge_category_file(path, "w", "x", "r", kept, [])
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual([i["title"] for i in data["domestic"]], ["기존 항목 하나", "새 항목 둘"])
            self.assertEqual(data["domestic"][0], old)  # 기존 항목 바이트 단위 보존(필드 포함)

            index = load_existing_index(Path(t))
            kept2, _ = run(incoming, index)
            self.assertEqual(kept2, [])
            merge_category_file(path, "w", "x", "r", kept2, [])
            self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))["domestic"]), 2)

    def test_no_rewrite_when_nothing_new(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "f.json"
            path.write_text('{"domestic": [], "global": []}', encoding="utf-8")
            merge_category_file(path, "w", "x", "r", [], [])
            self.assertEqual(path.read_text(encoding="utf-8"), '{"domestic": [], "global": []}')


class IndexScopeTests(unittest.TestCase):
    def test_only_weekly_article_files_are_indexed(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            art = {"domestic": [item(title="주간 기사 제목")], "global": []}
            (d / "2026-09-W3_x.json").write_text(json.dumps(art, ensure_ascii=False), encoding="utf-8")
            noise = {"trend": {"executive_summary": [], "items": [{"title": "(placeholder)"}]},
                     "title": "2026년 10월 하이엔드 주거 트렌드 리포트", "status": "coming_soon"}
            for name in ("2026-10_monthly.json", "2026-09_monthly.json", "2026-08_global_case.json",
                         "insight_index.json"):
                (d / name).write_text(json.dumps(noise, ensure_ascii=False), encoding="utf-8")
            idx = load_existing_index(d)
            self.assertEqual([e["title"] for e in idx], ["주간 기사 제목"])

    def test_real_data_dir_has_no_monthly_entries(self):
        real = Path(__file__).resolve().parent.parent / "data"
        titles = {e["title"] for e in load_existing_index(real)}
        self.assertNotIn("(placeholder)", titles)
        self.assertNotIn("2026년 10월 하이엔드 주거 트렌드 리포트", titles)


class InsightIndexPreserveTests(unittest.TestCase):
    def _with_index(self, initial):
        import contextlib

        @contextlib.contextmanager
        def ctx():
            with tempfile.TemporaryDirectory() as t:
                p = Path(t) / "insight_index.json"
                p.write_text(json.dumps(initial, ensure_ascii=False), encoding="utf-8")
                orig = run_lifestyle.INDEX_PATH
                run_lifestyle.INDEX_PATH = p
                try:
                    yield p
                finally:
                    run_lifestyle.INDEX_PATH = orig

        return ctx()

    def test_existing_weeks_and_sections_preserved_partial_and_rerun(self):
        initial = {
            "generated_on": "2026-10-02",
            "default": "2026-09-W2",
            "weeks": [
                {"week": "2026-09-W3", "year": 2026, "month": 9, "label": "l3", "range_label": "r3",
                 "sections": ["trend", "amenity", "architecture"]},
                {"week": "2026-09-W2", "year": 2026, "month": 9, "label": "l2", "range_label": "r2",
                 "sections": ["trend", "amenity", "facility", "global_case"]},
            ],
        }
        with self._with_index(initial) as p:
            run_lifestyle._update_insight_index(PERIOD, ["architecture", "wellness"])
            run_lifestyle._update_insight_index(PERIOD, ["architecture", "wellness"])  # 재실행
            out = json.loads(p.read_text(encoding="utf-8"))
        w3, w2 = out["weeks"]
        self.assertEqual(w3["sections"], ["trend", "amenity", "architecture", "wellness"])
        self.assertEqual(w2, initial["weeks"][1])  # 다른 주차는 그대로
        self.assertEqual(len(out["weeks"]), 2)

    def test_empty_saved_list_changes_no_sections(self):
        initial = {"generated_on": "x", "default": "d",
                   "weeks": [{"week": "2026-09-W3", "sections": ["trend"]}]}
        with self._with_index(initial) as p:
            run_lifestyle._update_insight_index(PERIOD, [])
            out = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(out["weeks"][0]["sections"], ["trend"])

    def test_new_week_is_prepended_without_touching_old(self):
        initial = {"generated_on": "x", "default": "d",
                   "weeks": [{"week": "2026-09-W2", "sections": ["trend"]}]}
        with self._with_index(initial) as p:
            run_lifestyle._update_insight_index(PERIOD, ["culture"])
            out = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual([w["week"] for w in out["weeks"]], ["2026-09-W3", "2026-09-W2"])
        self.assertEqual(out["weeks"][1], {"week": "2026-09-W2", "sections": ["trend"]})


class CategoryIsolationTests(unittest.TestCase):
    def test_collect_failure_isolated_and_partial_save(self):
        import collect_lifestyle as cl

        def fake_collect(client, period, key):
            if key == "wellness":
                raise RuntimeError("api down")
            return {"range_label": "r", "domestic": [item(title=f"{key} 국내 기사", url=f"https://a.com/{key}d")],
                    "global": [item(title=f"{key} 해외 기사", url=f"https://a.com/{key}g")]}

        orig_collect, orig_client = cl.collect_category, cl.Anthropic
        cl.collect_category, cl.Anthropic = fake_collect, lambda: None
        try:
            failures = {}
            res = cl.collect_lifestyle(PERIOD, failures)
        finally:
            cl.collect_category, cl.Anthropic = orig_collect, orig_client
        self.assertNotIn("wellness", res)
        self.assertEqual(len(res), 9)
        self.assertIn("wellness", failures)

    def test_run_main_partial_save_and_rerun(self):
        import os

        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            (base / "data").mkdir()
            cats = {"architecture": {"range_label": "r",
                                     "domestic": [item(title="건축 국내 기사 하나", url="https://a.com/ad")],
                                     "global": [item(title="건축 해외 기사 하나", url="https://a.com/ag", d="2026-10-30")]}}
            saved = (run_lifestyle.DATA_DIR, run_lifestyle.INDEX_PATH, run_lifestyle.BASE_DIR,
                     run_lifestyle.collect_lifestyle, run_lifestyle.get_last_completed_week_period,
                     run_lifestyle.filter_items)
            run_lifestyle.DATA_DIR, run_lifestyle.INDEX_PATH, run_lifestyle.BASE_DIR = (
                base / "data", base / "data" / "insight_index.json", base)
            run_lifestyle.get_last_completed_week_period = lambda: PERIOD

            def fake_collect(period, failures):
                failures["wellness"] = "RuntimeError: api down"
                return cats

            run_lifestyle.collect_lifestyle = fake_collect
            from content_quality import filter_items as real_filter
            run_lifestyle.filter_items = lambda *a, **k: real_filter(*a, **k, fetch=lambda u: 200) if False else real_filter(
                *a, fetch=lambda u: 200, **k)
            os.environ["ANTHROPIC_API_KEY"] = "test"
            try:
                run_lifestyle.main()
                run_lifestyle.main()  # 재실행
            finally:
                (run_lifestyle.DATA_DIR, run_lifestyle.INDEX_PATH, run_lifestyle.BASE_DIR,
                 run_lifestyle.collect_lifestyle, run_lifestyle.get_last_completed_week_period,
                 run_lifestyle.filter_items) = saved
            data = json.loads((base / "data" / "2026-09-W3_architecture.json").read_text(encoding="utf-8"))
            self.assertEqual(len(data["domestic"]), 1)  # 재실행 후에도 1건
            self.assertEqual(data["global"], [])  # 기간 밖 제외
            self.assertFalse((base / "data" / "2026-09-W3_wellness.json").exists())
            idx = json.loads((base / "data" / "insight_index.json").read_text(encoding="utf-8"))
            self.assertEqual(idx["weeks"][0]["sections"], ["architecture"])
            log = json.loads((base / "logs" / "2026-09-W3_collect_log.json").read_text(encoding="utf-8"))
            self.assertEqual(len(log["runs"]), 2)
            self.assertIn("wellness", log["runs"][0]["collect_failed"])
            reasons = [l["reason"] for l in log["runs"][0]["items"] if l["action"] == "excluded"]
            self.assertIn("date_out_of_range", reasons)
            self.assertIn("duplicate_url", [l["reason"] for l in log["runs"][1]["items"]])


if __name__ == "__main__":
    unittest.main()

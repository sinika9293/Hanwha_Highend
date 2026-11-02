import hashlib
import json
import os
import tempfile
import unittest
from datetime import date
from unittest import mock
from pathlib import Path

import collect_insight as ci
import content_quality as cq
import run_insight
import run_lifestyle
from dates import WeekPeriod

PERIOD = WeekPeriod(year=2026, week_label="2026-09-W3", start=date(2026, 9, 21), end=date(2026, 9, 27))
REAL_DATA = Path(__file__).resolve().parent.parent / "data"
SECTIONS = ("trend", "amenity", "facility", "global_case")


def mk(section, title, url="https://a.com/x", d="2026-09-23", **extra):
    base = {
        "trend": {"title": title, "summary": "s", "source": "m", "source_url": url, "date": d},
        "amenity": {"title": title, "summary": "s", "facility_type": "라운지", "source": "m",
                    "source_url": url, "date": d},
        "facility": {"name": title, "trend": "신규 등장", "summary": "s", "source_url": url, "date": d},
        "global_case": {"project": title, "location": "서울", "developer": "dev", "summary": "s",
                        "highlight": "h", "source_url": url, "date": d},
    }[section]
    base.update(extra)
    return base


def payload(section, dom, glob):
    if section == "facility":
        return {"range_label": "r", "domestic": {"popular": dom, "emerging": []},
                "global": {"popular": glob, "emerging": []}}
    return {"range_label": "r", "domestic": dom, "global": glob}


class SchemaTests(unittest.TestCase):
    def test_tools_for_four_sections(self):
        self.assertEqual(set(ci.INSIGHT_SUBMIT_TOOLS), set(SECTIONS))
        for key, tool in ci.INSIGHT_SUBMIT_TOOLS.items():
            self.assertEqual(tool["name"], f"submit_weekly_{key}")
            props = tool["input_schema"]["properties"]
            self.assertEqual(set(props), {"range_label", "domestic", "global"})

    def test_no_min_items_so_old_articles_never_pad(self):
        text = json.dumps(ci.INSIGHT_SUBMIT_TOOLS)
        self.assertNotIn("minItems", text)

    def test_facility_is_nested_others_are_arrays(self):
        for key, tool in ci.INSIGHT_SUBMIT_TOOLS.items():
            dom = tool["input_schema"]["properties"]["domestic"]
            if key == "facility":
                self.assertEqual(set(dom["properties"]), {"popular", "emerging"})
            else:
                self.assertEqual(dom["type"], "array")

    def test_every_item_requires_and_has_date(self):
        for key in SECTIONS:
            item = ci._ITEM_PROPS[key]
            self.assertIn("date", item)
            self.assertIn("date", ci._REQUIRED[key])

    def test_save_fields_match_real_weekly_files(self):
        for key in SECTIONS:
            path = REAL_DATA / f"2026-09-W3_{key}.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            for region in ("domestic", "global"):
                bucket = data[region]
                items = [i for g in bucket.values() for i in g] if isinstance(bucket, dict) else bucket
                self.assertTrue(items)
                for it in items:
                    self.assertLessEqual(set(it) - {"image_url"}, set(ci.SAVE_FIELDS[key]), (key, set(it)))

    def test_date_saved_only_for_trend(self):
        for key in ("amenity", "facility", "global_case"):
            self.assertNotIn("date", ci.SAVE_FIELDS[key])
        self.assertIn("date", ci.SAVE_FIELDS["trend"])

    def test_title_keys(self):
        self.assertEqual(ci.TITLE_KEY, {"trend": "title", "amenity": "title", "facility": "name",
                                        "global_case": "project"})


class StripAndFilterTests(unittest.TestCase):
    def test_strip_removes_date_for_non_trend(self):
        for key in ("amenity", "facility", "global_case"):
            out = run_insight._strip(key, [mk(key, "제목 하나")])
            self.assertNotIn("date", out[0])
        out = run_insight._strip("trend", [mk("trend", "제목 하나")])
        self.assertIn("date", out[0])

    def _filter(self, section, items, index=None, fetch=lambda u: 200):
        log = []
        idx = [] if index is None else index
        kept = cq.filter_items(items, PERIOD, idx, section, "domestic", log, fetch=fetch,
                               title_key=ci.TITLE_KEY[section], url_dedup_new=False)
        return kept, log

    def test_date_rules_all_sections(self):
        for key in SECTIONS:
            kept, log = self._filter(key, [
                mk(key, "기간 안 항목", url="https://a.com/1", d="2026-09-21"),
                mk(key, "기간 밖 항목", url="https://a.com/2", d="2026-09-28"),
                mk(key, "발행일 없음", url="https://a.com/3", d=""),
                mk(key, "발행일 불명", url="https://a.com/4", d="지난주"),
            ])
            self.assertEqual(len(kept), 1, key)
            self.assertEqual(sorted(l["reason"] for l in log if l["action"] == "excluded"),
                             ["date_missing", "date_out_of_range", "date_unparseable"], key)

    def test_same_url_different_items_allowed_in_same_run(self):
        for key in SECTIONS:
            kept, _ = self._filter(key, [mk(key, "서로 다른 첫번째 트렌드", url="https://a.com/same"),
                                         mk(key, "완전히 별개의 두번째 사례", url="https://a.com/same")])
            self.assertEqual(len(kept), 2, key)

    def test_same_run_same_title_still_deduped(self):
        kept, log = self._filter("facility", [mk("facility", "동일한 제목", url="https://a.com/1"),
                                              mk("facility", "동일한 제목", url="https://a.com/2")])
        self.assertEqual(len(kept), 1)
        self.assertEqual(log[0]["reason"], "duplicate_title")

    def test_prior_week_url_and_title_dedup(self):
        idx = [cq.make_index_entry("이전 주 기사", "https://a.com/old", "2026-09-15")]
        kept, log = self._filter("trend", [mk("trend", "새 제목이지만 같은 URL", url="https://www.a.com/old/?utm_x=1")], idx)
        self.assertEqual(kept, [])
        self.assertEqual(log[0]["reason"], "duplicate_url")
        kept, log = self._filter("trend", [mk("trend", "이전 주 기사", url="https://b.com/new")], idx)
        self.assertEqual(log[0]["reason"], "duplicate_title")

    def test_conservative_similarity_kept(self):
        idx = [cq.make_index_entry("한남4구역 삼성물산 한강뷰 설계안 공개", "https://a.com/1", "2026-09-01")]
        kept, log = self._filter("trend", [mk("trend", "한남4구역 삼성물산 한강뷰 설계안 전격 공개",
                                              url="https://b.com/2", d="2026-09-23")], idx)
        self.assertEqual(len(kept), 1)
        self.assertTrue(any(l["reason"] == "similar_title_kept" for l in log))

    def test_url_check_unverified_keeps_invalid_blanks(self):
        kept, log = self._filter("trend", [mk("trend", "제목 가나다")], fetch=lambda u: 403)
        self.assertEqual(kept[0]["source_url"], "https://a.com/x")
        self.assertEqual(log[0]["url_status"], "unverified")
        kept, log = self._filter("trend", [mk("trend", "제목 가나다")], fetch=lambda u: 404)
        self.assertEqual(kept[0]["source_url"], "")


class SectionIndexScopeTests(unittest.TestCase):
    def test_only_same_section_and_excludes_monthly_summary_week(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)

            def w(name, items):
                (d / name).write_text(json.dumps({"domestic": items, "global": []}, ensure_ascii=False),
                                      encoding="utf-8")

            w("2026-09-W2_trend.json", [mk("trend", "트렌드 이전주", url="https://a.com/t2")])
            w("2026-09-W3_trend.json", [mk("trend", "트렌드 이번주", url="https://a.com/t3")])
            w("2026-09-W2_amenity.json", [mk("amenity", "어메니티 이전주", url="https://a.com/t2")])
            w("2026-08-W4_trend.json", [{"title": "월간 종합 항목"}])
            w("2026-09_monthly.json", [{"title": "(placeholder)"}])
            idx = cq.load_section_index(d, "trend", "2026-09-W3", {"2026-08-W4"})
            titles = {e["title"]: e["url_dedup"] for e in idx}
            self.assertEqual(titles, {"트렌드 이전주": True, "트렌드 이번주": False})

    def test_other_section_url_does_not_exclude(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "2026-09-W2_amenity.json").write_text(
                json.dumps({"domestic": [mk("amenity", "어메니티 기사", url="https://a.com/shared")], "global": []},
                           ensure_ascii=False), encoding="utf-8")
            idx = cq.load_section_index(d, "global_case", "2026-09-W3")
            log = []
            kept = cq.filter_items([mk("global_case", "다른 섹션 프로젝트", url="https://a.com/shared")],
                                   PERIOD, idx, "global_case", "domestic", log, fetch=lambda u: 200,
                                   title_key="project", url_dedup_new=False)
            self.assertEqual(len(kept), 1)

    def test_count_items(self):
        self.assertEqual(cq.count_items([1, 2]), 2)
        self.assertEqual(cq.count_items({"popular": [1], "emerging": [2, 3]}), 3)
        self.assertEqual(cq.count_items(None), 0)


class MergeShapeTests(unittest.TestCase):
    def test_facility_nested_merge_preserves_existing(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "2026-09-W3_facility.json"
            old = {"name": "기존", "trend": "t", "summary": "s", "source_url": "u", "image_url": "img"}
            path.write_text(json.dumps({"week": "w", "category": "facility", "range_label": "r",
                                        "domestic": {"popular": [old], "emerging": []},
                                        "global": {"popular": [], "emerging": []}}, ensure_ascii=False),
                            encoding="utf-8")
            new = {"popular": [], "emerging": [{"name": "신규", "trend": "t", "summary": "s", "source_url": ""}]}
            cq.merge_category_file(path, "w", "facility", "r", new, {"popular": [], "emerging": []})
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["domestic"]["popular"], [old])
            self.assertEqual([i["name"] for i in data["domestic"]["emerging"]], ["신규"])

    def test_nothing_new_does_not_rewrite(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "f.json"
            text = '{"domestic": {"popular": [], "emerging": []}, "global": {"popular": [], "emerging": []}}'
            path.write_text(text, encoding="utf-8")
            empty = {"popular": [], "emerging": []}
            cq.merge_category_file(path, "w", "facility", "r", empty, empty)
            self.assertEqual(path.read_text(encoding="utf-8"), text)


class RunInsightTests(unittest.TestCase):
    def _run(self, base, fake_payloads, failures=None, period=PERIOD):
        saved = (run_insight.DATA_DIR, run_insight.BASE_DIR, run_lifestyle.INDEX_PATH,
                 run_insight.collect_insight, run_insight.get_last_completed_week_period,
                 run_insight.filter_items)
        run_insight.DATA_DIR, run_insight.BASE_DIR = base / "data", base
        run_lifestyle.INDEX_PATH = base / "data" / "insight_index.json"
        run_insight.get_last_completed_week_period = lambda: period

        sentinel = object()
        received = []

        def fake_collect(p, fails, *, backend):  # backend는 필수 키워드: 전달되지 않으면 TypeError
            received.append(backend)
            fails.update(failures or {})
            return fake_payloads

        run_insight.collect_insight = fake_collect
        real = cq.filter_items
        run_insight.filter_items = lambda *a, **k: real(*a, fetch=lambda u: 200, **k)
        patcher = mock.patch.object(run_insight, "prepare_backend", lambda log=print: (sentinel, None))
        patcher.start()
        self.addCleanup(patcher.stop)
        try:
            self.assertEqual(run_insight.main(), 0)
            self.assertEqual(len(received), 1)
            self.assertIs(received[0], sentinel)  # prepare_backend 반환 객체가 수집 함수에 그대로 전달됨
        finally:
            (run_insight.DATA_DIR, run_insight.BASE_DIR, run_lifestyle.INDEX_PATH,
             run_insight.collect_insight, run_insight.get_last_completed_week_period,
             run_insight.filter_items) = saved

    def _setup(self, t):
        base = Path(t)
        (base / "data").mkdir()
        (base / "data" / "insight_index.json").write_text(json.dumps({
            "generated_on": "x", "default": "2026-09-W2",
            "weeks": [{"week": "2026-09-W2", "sections": list(SECTIONS)}]}), encoding="utf-8")
        return base

    def _payloads(self):
        return {k: payload(k, [mk(k, f"{k} 국내 기사", url=f"https://a.com/{k}d")],
                           [mk(k, f"{k} 해외 기사", url=f"https://a.com/{k}g")]) for k in SECTIONS}

    def test_all_four_sections_saved_with_existing_schema(self):
        with tempfile.TemporaryDirectory() as t:
            base = self._setup(t)
            self._run(base, self._payloads())
            for k in SECTIONS:
                data = json.loads((base / "data" / f"2026-09-W3_{k}.json").read_text(encoding="utf-8"))
                self.assertEqual(set(data), {"week", "category", "range_label", "domestic", "global"})
                self.assertEqual(data["category"], k)
                items = ([i for g in data["domestic"].values() for i in g] if k == "facility"
                         else data["domestic"])
                self.assertEqual(len(items), 1)
                self.assertLessEqual(set(items[0]), set(ci.SAVE_FIELDS[k]))
                if k != "trend":
                    self.assertNotIn("date", items[0])
            idx = json.loads((base / "data" / "insight_index.json").read_text(encoding="utf-8"))
            self.assertEqual(idx["weeks"][0]["week"], "2026-09-W3")
            self.assertEqual(idx["weeks"][0]["sections"], list(SECTIONS))
            self.assertEqual(idx["weeks"][1]["sections"], list(SECTIONS))  # 기존 주차 보존

    def test_partial_failure_saves_others(self):
        with tempfile.TemporaryDirectory() as t:
            base = self._setup(t)
            pl = self._payloads()
            del pl["amenity"]
            self._run(base, pl, failures={"amenity": "RuntimeError: api down"})
            self.assertFalse((base / "data" / "2026-09-W3_amenity.json").exists())
            for k in ("trend", "facility", "global_case"):
                self.assertTrue((base / "data" / f"2026-09-W3_{k}.json").exists())
            idx = json.loads((base / "data" / "insight_index.json").read_text(encoding="utf-8"))
            self.assertNotIn("amenity", idx["weeks"][0]["sections"])
            log = json.loads((base / "logs" / "2026-09-W3_collect_log.json").read_text(encoding="utf-8"))
            self.assertIn("amenity", log["runs"][0]["collect_failed"])

    def test_rerun_is_idempotent(self):
        with tempfile.TemporaryDirectory() as t:
            base = self._setup(t)
            self._run(base, self._payloads())
            before = {k: (base / "data" / f"2026-09-W3_{k}.json").read_text(encoding="utf-8") for k in SECTIONS}
            self._run(base, self._payloads())
            for k in SECTIONS:
                self.assertEqual((base / "data" / f"2026-09-W3_{k}.json").read_text(encoding="utf-8"), before[k])
            log = json.loads((base / "logs" / "2026-09-W3_collect_log.json").read_text(encoding="utf-8"))
            self.assertEqual(len(log["runs"]), 2)

    def test_shortfall_and_empty_are_logged_not_padded(self):
        with tempfile.TemporaryDirectory() as t:
            base = self._setup(t)
            pl = self._payloads()
            pl["trend"] = payload("trend", [mk("trend", "기간 밖", d="2026-01-01")], [])
            self._run(base, pl)
            self.assertFalse((base / "data" / "2026-09-W3_trend.json").exists())  # 빈 결과는 파일 생성 안 함
            log = json.loads((base / "logs" / "2026-09-W3_collect_log.json").read_text(encoding="utf-8"))
            items = log["runs"][0]["items"]
            self.assertTrue(any(i.get("action") == "empty_result" and i["category"] == "trend" for i in items))
            self.assertTrue(any(i.get("action") == "shortfall" and i["category"] == "amenity" for i in items))
            self.assertTrue(any(i.get("reason") == "date_out_of_range" for i in items))

    def test_monthly_summary_week_skipped(self):
        with tempfile.TemporaryDirectory() as t:
            base = self._setup(t)
            w4 = WeekPeriod(year=2026, week_label="2026-08-W4", start=date(2026, 8, 24), end=date(2026, 8, 30))

            def boom(*a, **k):
                raise AssertionError("must not collect")

            saved = run_insight.collect_insight
            run_insight.collect_insight = boom
            try:
                saved_cfg = (run_insight.DATA_DIR, run_insight.BASE_DIR, run_insight.get_last_completed_week_period)
                run_insight.DATA_DIR, run_insight.BASE_DIR = base / "data", base
                run_insight.get_last_completed_week_period = lambda: w4
                patcher = mock.patch.object(run_insight, "prepare_backend", lambda log=print: (object(), None))
                patcher.start()
                self.addCleanup(patcher.stop)
                run_insight.main()
            finally:
                run_insight.collect_insight = saved
                (run_insight.DATA_DIR, run_insight.BASE_DIR, run_insight.get_last_completed_week_period) = saved_cfg
            self.assertEqual(list((base / "data").glob("2026-08-W4_*")), [])
            self.assertFalse((base / "logs").exists())

    def test_existing_items_preserved_on_rerun_with_new_item(self):
        with tempfile.TemporaryDirectory() as t:
            base = self._setup(t)
            self._run(base, self._payloads())
            path = base / "data" / "2026-09-W3_global_case.json"
            first = json.loads(path.read_text(encoding="utf-8"))
            pl = self._payloads()
            pl["global_case"]["domestic"].append(mk("global_case", "새로 나온 프로젝트", url="https://a.com/gcd"))
            self._run(base, pl)  # 같은 URL이지만 다른 프로젝트 → 허용
            second = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(second["domestic"][0], first["domestic"][0])
            self.assertEqual(len(second["domestic"]), 2)
            self.assertEqual(second["global"], first["global"])


class CollectIsolationTests(unittest.TestCase):
    def test_collect_insight_isolates_section_failure(self):
        orig = ci.collect_insight_section

        sentinel = object()
        seen = []

        def fake(backend, period, section):
            seen.append(backend)
            if section == "facility":
                raise RuntimeError("boom")
            return payload(section, [], [])

        ci.collect_insight_section = fake
        try:
            fails = {}
            res = ci.collect_insight(PERIOD, fails, backend=sentinel)
        finally:
            ci.collect_insight_section = orig
        self.assertEqual(len(seen), len(SECTIONS))
        self.assertTrue(all(b is sentinel for b in seen))  # 모든 섹션 호출에 같은 백엔드 전달
        self.assertEqual(set(res), {"trend", "amenity", "global_case"})
        self.assertIn("facility", fails)


# 백엔드 분리 전 기준 커밋(39e4f7c)의 INSIGHT_RESEARCH_PROMPTS 템플릿 SHA-256
GOLDEN_RESEARCH_PROMPT_SHA256 = {
    "trend": "36f9e66fae54b523a5c2492246afc38f664f3bc9948bf7a84a2b791b6131b389",
    "amenity": "b41c0a69981ee0c2d3c71522a83624af07d8d3f7d7a78357bdc6e5fdf7fea862",
    "facility": "880212e859b42933d3a1ab302c6193ea9d1b23405c4940424079bfc7e8be0047",
    "global_case": "5599f08aa986a3b6bd7c67bc1eec0c4893dd67a26c3f0bcb84b80ed7315bd4f5",
}


class RecordingBackend:
    name = "recording"

    def __init__(self, memo="조사 메모 본문", structured=None):
        self.memo, self.structured = memo, structured if structured is not None else {"ok": True}
        self.research_calls, self.structure_calls = [], []

    def available(self):
        return True, "ok"

    def research(self, user_prompt, *, max_searches=None):
        self.research_calls.append((user_prompt, max_searches))
        return self.memo

    def structure(self, user_prompt, tool):
        self.structure_calls.append((user_prompt, tool))
        return self.structured


class PromptPassThroughTests(unittest.TestCase):
    def test_research_templates_unchanged_from_baseline(self):
        for section in SECTIONS:
            digest = hashlib.sha256(ci.INSIGHT_RESEARCH_PROMPTS[section].encode("utf-8")).hexdigest()
            self.assertEqual(digest, GOLDEN_RESEARCH_PROMPT_SHA256[section], section)

    def test_research_insight_passes_formatted_prompt_only(self):
        for section in SECTIONS:
            with self.subTest(section=section):
                backend = RecordingBackend()
                memo = ci.research_insight(backend, PERIOD, section)
                expected = ci.INSIGHT_RESEARCH_PROMPTS[section].format(
                    range_label=PERIOD.range_label, label=PERIOD.week_label)
                self.assertEqual(memo, "조사 메모 본문")
                self.assertEqual(backend.research_calls, [(expected, None)])  # 검색 횟수는 어댑터 기본값 사용
                self.assertIn(PERIOD.week_label, expected)

    def test_research_insight_empty_memo_still_raises(self):
        with self.assertRaises(RuntimeError):
            ci.research_insight(RecordingBackend(memo="  "), PERIOD, "trend")

    def test_structure_insight_passes_exact_user_content_and_tool(self):
        memo = "메모 내용\n둘째 줄"
        for section in SECTIONS:
            with self.subTest(section=section):
                backend = RecordingBackend(structured={"range_label": "r"})
                result = ci.structure_insight(backend, PERIOD, section, memo)
                expected = (
                    f"대상 기간: {PERIOD.range_label} ({PERIOD.week_label})\n"
                    "각 항목의 date에는 메모에 적힌 기사 발행일을 YYYY-MM-DD로 옮기고, "
                    "메모에 발행일이 없으면 빈 문자열로 두세요. 기간 밖 기사는 포함하지 마세요.\n\n"
                    f"조사 메모:\n{memo}"
                )
                self.assertEqual(result, {"range_label": "r"})
                self.assertEqual(len(backend.structure_calls), 1)
                prompt, tool = backend.structure_calls[0]
                self.assertEqual(prompt, expected)
                self.assertIs(tool, ci.INSIGHT_SUBMIT_TOOLS[section])

    def test_collect_insight_section_chains_memo_into_structure(self):
        backend = RecordingBackend(memo="연결 확인 메모")
        ci.collect_insight_section(backend, PERIOD, "amenity")
        self.assertEqual(len(backend.research_calls), 1)
        self.assertIn("연결 확인 메모", backend.structure_calls[0][0])


if __name__ == "__main__":
    unittest.main()

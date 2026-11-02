"""run_insight / run_lifestyle 와 상태 기록(ops_state)의 통합 테스트.

AI 호출·네트워크 없음: 수집 함수와 URL 검사는 가짜로 대체하고, 모든 경로는 임시 폴더를 쓴다.
"""

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import backends
import content_quality as cq
import ops_state
import run_insight
import run_lifestyle
from dates import WeekPeriod
from lifestyle_schema import LIFESTYLE_KEYS
from schema import SECTION_KEYS

ROOT = Path(__file__).resolve().parent.parent
W3 = WeekPeriod(year=2026, week_label="2026-09-W3", start=date(2026, 9, 21), end=date(2026, 9, 27))
W4_SUMMARY = WeekPeriod(year=2026, week_label="2026-08-W4", start=date(2026, 8, 24), end=date(2026, 8, 30))
W2_LEGACY = WeekPeriod(year=2026, week_label="2026-09-W2", start=date(2026, 9, 14), end=date(2026, 9, 20))
TITLE_A = "한남동 신규 레지던스 단독 소식 하나"
TITLE_B = "성수동 복합 개발 소식 둘째 기사"


class FakeBackend:
    name = "fake"

    def available(self):
        return True, "ok"


def item(title, url, d="2026-09-23"):
    return {"title": title, "summary": "요약", "source": "매체", "source_url": url, "date": d}


def cat(domestic=(), global_=()):
    return {"range_label": "r", "domestic": list(domestic), "global": list(global_)}


def facility_item(name, url):
    return {"name": name, "trend": "신규 등장", "summary": "요약", "source_url": url, "date": "2026-09-23"}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Harness(unittest.TestCase):
    """run_* 모듈의 경로를 임시 폴더로 바꾸고 main() 을 실행한다."""

    @classmethod
    def setUpClass(cls):
        cls._repo_ops_before = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "ops").rglob("*"))
        cls._repo_logs_exist = (ROOT / "logs").exists()

    @classmethod
    def tearDownClass(cls):
        after = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "ops").rglob("*"))
        assert after == cls._repo_ops_before, "tests must not write into the repository ops/"
        assert (ROOT / "logs").exists() == cls._repo_logs_exist, "tests must not create repository logs/"

    def run_main(self, module, base, payloads, failures=None, period=W3, backend=None,
                 filter_hook=None, extra=()):
        failures = failures or {}
        real_filter = cq.filter_items
        collect_name = "collect_insight" if module is run_insight else "collect_lifestyle"
        calls = []

        def fake_collect(p, fails, *, backend):
            calls.append(backend)
            fails.update(failures)
            return {k: v for k, v in payloads.items() if k not in failures}

        def fake_filter(*a, **k):
            if filter_hook:
                filter_hook(a)
            return real_filter(*a, fetch=lambda u: 200, **k)

        out = io.StringIO()
        with contextlib.ExitStack() as st:
            for target in {module, run_lifestyle}:
                st.enter_context(mock.patch.object(target, "BASE_DIR", base))
                st.enter_context(mock.patch.object(target, "DATA_DIR", base / "data"))
            st.enter_context(mock.patch.object(run_lifestyle, "INDEX_PATH", base / "data" / "insight_index.json"))
            st.enter_context(mock.patch.object(module, "get_last_completed_week_period", lambda: period))
            st.enter_context(mock.patch.object(module, "prepare_backend",
                                               lambda log=print: (backend or FakeBackend(), None)))
            st.enter_context(mock.patch.object(module, collect_name, fake_collect))
            st.enter_context(mock.patch.object(module, "filter_items", fake_filter))
            for cm in extra:
                st.enter_context(cm)
            with contextlib.redirect_stdout(out):
                code = module.main()
        self.last_collect_calls = calls
        return code, out.getvalue()

    def status(self, base, week="2026-09-W3"):
        return ops_state.read_week(base / "ops", week)

    def lifestyle_payloads(self, **overrides):
        payloads = {k: cat() for k in LIFESTYLE_KEYS}
        payloads.update(overrides)
        return payloads

    def insight_payloads(self, **overrides):
        payloads = {k: cat() for k in SECTION_KEYS}
        payloads["facility"] = {"range_label": "r", "domestic": {"popular": [], "emerging": []},
                                "global": {"popular": [], "emerging": []}}
        payloads.update(overrides)
        return payloads


class LifestyleStatusTests(Harness):
    def test_saved_empty_failed_and_counts(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            payloads = self.lifestyle_payloads(
                architecture=cat([item(TITLE_A, "https://a.com/1"), item(TITLE_B, "https://a.com/2")]),
                culture=cat([item("범위 밖 기사 제목 입니다 하나", "https://a.com/3", "2026-08-01"),
                             item("날짜 없는 기사 제목 입니다 하나", "https://a.com/4", "")]),
            )
            code, out = self.run_main(run_lifestyle, base, payloads, failures={"wellness": "RuntimeError: api down"})
            self.assertEqual(code, 0)
            sections = self.status(base)["sections"]
            self.assertEqual(sorted(sections), sorted(LIFESTYLE_KEYS))
            arch = sections["architecture"]["effective"]
            self.assertEqual((arch["status"], arch["settled"], arch["settled_reason"]), ("saved", True, "all_sources_ok"))
            counts = sections["architecture"]["attempts"][0]["counts"]
            self.assertEqual((counts["saved_new"], counts["saved_total"]), (2, 2))
            cult = sections["culture"]["effective"]
            self.assertEqual((cult["status"], cult["settled"], cult["settled_reason"]), ("empty", True, "all_sources_ok"))
            cc = sections["culture"]["attempts"][0]["counts"]
            self.assertEqual((cc["saved_new"], cc["saved_total"]), (0, 0))
            self.assertEqual((cc["excluded"]["date_out_of_range"], cc["excluded"]["date_missing"]), (1, 1))
            well = sections["wellness"]["effective"]
            self.assertEqual((well["status"], well["settled"], well["settled_reason"]), ("failed", False, "all_sources_failed"))
            wa = sections["wellness"]["attempts"][0]
            self.assertIsNone(wa["counts"]["saved_total"])
            self.assertEqual(wa["units"][0]["error_type"], "RuntimeError")
            self.assertEqual(wa["units"][0]["id"], "ai_backend:fake")
            self.assertIn("FAILED wellness", out)  # 자유 문장 오류는 콘솔/로컬 로그에만 남는다

    def test_rerun_counts_saved_new_and_total_and_keeps_data(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            payloads = self.lifestyle_payloads(architecture=cat([item(TITLE_A, "https://a.com/1")]))
            self.run_main(run_lifestyle, base, payloads)
            data_file = base / "data" / "2026-09-W3_architecture.json"
            first = sha(data_file)
            self.run_main(run_lifestyle, base, payloads)
            self.assertEqual(sha(data_file), first)
            entry = self.status(base)["sections"]["architecture"]
            self.assertEqual(len(entry["attempts"]), 2)
            second = entry["attempts"][1]["counts"]
            self.assertEqual((second["saved_new"], second["saved_total"]), (0, 1))
            self.assertEqual(second["excluded"]["duplicate_url"], 1)
            self.assertEqual((entry["effective"]["status"], entry["effective"]["settled"]), ("saved", True))

    def test_settled_effective_survives_later_failure(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            payloads = self.lifestyle_payloads(architecture=cat([item(TITLE_A, "https://a.com/1")]))
            self.run_main(run_lifestyle, base, payloads)
            first_run = self.status(base)["sections"]["architecture"]["effective"]["run_id"]
            self.run_main(run_lifestyle, base, payloads, failures={"architecture": "TimeoutError: slow"})
            entry = self.status(base)["sections"]["architecture"]
            self.assertEqual([a["status"] for a in entry["attempts"]], ["saved", "failed"])
            self.assertEqual(entry["effective"]["run_id"], first_run)
            self.assertTrue(entry["effective"]["settled"])

    def test_processing_error_is_recorded_without_message(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)

            def boom(args):
                if args[3] == "culture":  # filter_items(items, period, index, category, ...)
                    raise ValueError("secret detail https://leak.example.com sk-ant-" + "zzzzzzzzzzzz")

            payloads = self.lifestyle_payloads(architecture=cat([item(TITLE_A, "https://a.com/1")]),
                                               culture=cat([item(TITLE_B, "https://a.com/2")]))
            code, out = self.run_main(run_lifestyle, base, payloads, filter_hook=boom)
            self.assertEqual(code, 0)
            sections = self.status(base)["sections"]
            cult = sections["culture"]
            self.assertEqual((cult["effective"]["status"], cult["effective"]["settled_reason"]), ("failed", "processing_error"))
            self.assertEqual(cult["attempts"][0]["units"][0]["error_type"], "ValueError")
            self.assertEqual(cult["attempts"][0]["units"][0]["error_code"], "processing_error")
            self.assertEqual(sections["architecture"]["effective"]["status"], "saved")  # 다른 카테고리는 격리
            text = ops_state.status_path(base / "ops", "2026-09-W3").read_text(encoding="utf-8")
            for needle in ("secret detail", "leak.example.com", "sk-ant"):
                self.assertNotIn(needle, text)
            self.assertFalse((base / "data" / "2026-09-W3_culture.json").exists())

    def test_settled_week_is_not_locked_in_p4(self):
        # P4 는 정산 주차의 데이터 잠금을 구현하지 않는다: 재실행의 신규 항목은 기존처럼 병합된다.
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            self.run_main(run_lifestyle, base, self.lifestyle_payloads(architecture=cat([item(TITLE_A, "https://a.com/1")])))
            self.run_main(run_lifestyle, base, self.lifestyle_payloads(
                architecture=cat([item(TITLE_A, "https://a.com/1"), item(TITLE_B, "https://a.com/2")])))
            data = json.loads((base / "data" / "2026-09-W3_architecture.json").read_text(encoding="utf-8"))
            self.assertEqual(len(data["domestic"]), 2)
            counts = self.status(base)["sections"]["architecture"]["attempts"][1]["counts"]
            self.assertEqual((counts["saved_new"], counts["saved_total"]), (1, 2))

    def test_status_write_failure_returns_1_after_data_is_saved(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            failing = mock.patch.object(ops_state, "write_json_atomic",
                                        side_effect=ops_state.StatusWriteError("write_failed:OSError"))
            code, out = self.run_main(run_lifestyle, base, self.lifestyle_payloads(
                architecture=cat([item(TITLE_A, "https://a.com/1")])), extra=[failing])
            self.assertEqual(code, 1)
            self.assertIn("STATUS_WRITE_FAILED", out)
            self.assertTrue((base / "data" / "2026-09-W3_architecture.json").exists())
            state = ops_state.section_state(base / "ops", "2026-09-W3", "architecture")
            self.assertEqual((state["status"], state["settled_reason"]), ("unprocessed", "not_run"))
            locks = list((base / "logs" / ".locks").iterdir())
            self.assertEqual([p.name for p in locks], ["2026-09-W3.lock"])  # 영구 락 파일은 남고
            with ops_state.week_lock(base / "logs" / ".locks", "2026-09-W3", timeout=1):  # 잠금은 해제되어 있다
                pass

    def test_multi_classified_and_shadow_counts_are_aggregated(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            same = item(TITLE_A, "https://shared.com/a")
            self.run_main(run_lifestyle, base, self.lifestyle_payloads(architecture=cat([same]), culture=cat([same])))
            sections = self.status(base)["sections"]
            self.assertEqual(sections["architecture"]["attempts"][0]["counts"]["multi_classified"], 1)
            self.assertEqual(sections["culture"]["attempts"][0]["counts"]["multi_classified"], 1)


class InsightStatusTests(Harness):
    def test_saved_empty_skip_and_facility_total(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            payloads = self.insight_payloads(
                trend=cat([item(TITLE_A, "https://a.com/1")]),
                facility={"range_label": "r",
                          "domestic": {"popular": [facility_item(TITLE_B, "https://a.com/2")], "emerging": []},
                          "global": {"popular": [], "emerging": [facility_item("해외 시설 신규 등장 사례 하나", "https://a.com/3")]}},
            )
            code, _ = self.run_main(run_insight, base, payloads)
            self.assertEqual(code, 0)
            sections = self.status(base)["sections"]
            self.assertEqual(sorted(sections), sorted(SECTION_KEYS))
            trend = sections["trend"]["attempts"][0]
            self.assertEqual((trend["kind"], trend["status"], trend["settled"]), ("insight", "saved", True))
            self.assertEqual((trend["counts"]["saved_new"], trend["counts"]["saved_total"]), (1, 1))
            fac = sections["facility"]["attempts"][0]["counts"]
            self.assertEqual((fac["saved_new"], fac["saved_total"]), (2, 2))  # {popular, emerging} 합산
            amenity = sections["amenity"]["effective"]
            self.assertEqual((amenity["status"], amenity["settled"]), ("empty", True))  # 신규 0건 + 파일 없음
            self.assertFalse((base / "data" / "2026-09-W3_amenity.json").exists())  # 기존 동작 유지: 파일을 만들지 않음

    def test_collect_failure_for_one_section_only(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            code, _ = self.run_main(run_insight, base, self.insight_payloads(trend=cat([item(TITLE_A, "https://a.com/1")])),
                                    failures={"global_case": "RuntimeError: api down"})
            sections = self.status(base)["sections"]
            self.assertEqual(sections["global_case"]["effective"]["settled_reason"], "all_sources_failed")
            self.assertEqual(sections["trend"]["effective"]["status"], "saved")

    def test_monthly_summary_week_writes_no_status(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            (base / "ops").mkdir()
            (base / "ops" / "legacy_weeks.json").write_text(json.dumps(
                {"schema_version": 1, "weeks": {"2026-08-W4": {"reason": "legacy_aggregate", "approved": True}}}), encoding="utf-8")
            code, out = self.run_main(run_insight, base, self.insight_payloads(), period=W4_SUMMARY)
            self.assertEqual(code, 0)
            self.assertEqual(self.last_collect_calls, [])
            self.assertFalse((base / "ops" / "status").exists())  # 기존 월간 종합 건너뛰기가 먼저 적용된다
            self.assertFalse((base / "logs").exists())


class LegacyGuardTests(Harness):
    def registry(self, base, week="2026-09-W3", approved=True, reason="legacy_label_conflict"):
        (base / "ops").mkdir(exist_ok=True)
        (base / "ops" / "legacy_weeks.json").write_text(json.dumps(
            {"schema_version": 1, "weeks": {week: {"reason": reason, "approved": approved}}}),
            encoding="utf-8")

    def seed_week(self, base, prefix="2026-09-W3"):
        data = base / "data"
        data.mkdir(parents=True, exist_ok=True)
        files = {}
        for key in ("trend", "architecture"):
            path = data / f"{prefix}_{key}.json"
            path.write_text(json.dumps({"week": prefix, "category": key, "range_label": "old range",
                                        "domestic": [item(TITLE_A, "https://old.com/x")], "global": []}, ensure_ascii=False),
                            encoding="utf-8")
            files[path] = sha(path)
        index = data / "insight_index.json"
        index.write_text(json.dumps({"generated_on": "x", "default": "2026-09-W2", "weeks": []}), encoding="utf-8")
        files[index] = sha(index)
        return files

    def check_blocked(self, module, keys, period=W3, reason="legacy_label_conflict"):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            self.registry(base, week=period.week_label, reason=reason)
            hashes = self.seed_week(base, period.week_label)
            payloads = self.insight_payloads() if module is run_insight else self.lifestyle_payloads()
            code, out = self.run_main(module, base, payloads, period=period)
            self.assertEqual(code, 1)  # 레거시 가드로 수집이 차단되면 종료 코드 1
            self.assertEqual(self.last_collect_calls, [])  # AI 호출 없음
            for path, digest in hashes.items():
                self.assertEqual(sha(path), digest, path.name)  # 데이터·인덱스 해시 불변
            self.assertEqual(sorted(p.name for p in (base / "data").iterdir()), sorted(p.name for p in hashes))
            sections = self.status(base, period.week_label)["sections"]
            self.assertEqual(sorted(sections), sorted(keys))
            for key in keys:
                attempt = sections[key]["attempts"][0]
                self.assertEqual((attempt["status"], attempt["settled"], attempt["settled_reason"]),
                                 ("failed", False, reason))
                self.assertEqual(attempt["units"], [])
                self.assertIsNone(attempt["counts"]["saved_total"])
            self.assertFalse((base / "logs" / f"{period.week_label}_collect_log.json").exists())

    def test_registered_week_blocks_insight_run(self):
        self.check_blocked(run_insight, SECTION_KEYS)

    def test_registered_week_blocks_lifestyle_run(self):
        self.check_blocked(run_lifestyle, LIFESTYLE_KEYS)

    def test_w2_label_conflict_reason_and_exit_code(self):
        for module, keys in ((run_insight, SECTION_KEYS), (run_lifestyle, LIFESTYLE_KEYS)):
            with self.subTest(module=module.__name__):
                self.check_blocked(module, keys, period=W2_LEGACY, reason="legacy_label_conflict")

    def test_w4_aggregate_reason_and_exit_code(self):
        # 라이프스타일에는 월간 종합 건너뛰기 상수가 없으므로 레지스트리 가드가 적용되어 legacy_aggregate 로 기록된다.
        self.check_blocked(run_lifestyle, LIFESTYLE_KEYS, period=W4_SUMMARY, reason="legacy_aggregate")

    def test_repository_registry_maps_each_week_to_its_own_reason(self):
        registry_text = (ROOT / "ops" / "legacy_weeks.json").read_text(encoding="utf-8")
        cases = ((W2_LEGACY, "legacy_label_conflict"), (W4_SUMMARY, "legacy_aggregate"))
        for module, keys in ((run_insight, SECTION_KEYS), (run_lifestyle, LIFESTYLE_KEYS)):
            for period, expected in cases:
                if module is run_insight and period is W4_SUMMARY:
                    continue  # 인사이트의 월간 종합 건너뛰기가 먼저 적용된다(아래 테스트)
                with self.subTest(module=module.__name__, week=period.week_label), tempfile.TemporaryDirectory() as t:
                    base = Path(t)
                    (base / "ops").mkdir()
                    (base / "ops" / "legacy_weeks.json").write_text(registry_text, encoding="utf-8")
                    payloads = self.insight_payloads() if module is run_insight else self.lifestyle_payloads()
                    code, out = self.run_main(module, base, payloads, period=period)
                    self.assertEqual(code, 1)
                    self.assertEqual(self.last_collect_calls, [])
                    sections = self.status(base, period.week_label)["sections"]
                    self.assertEqual(sorted(sections), sorted(keys))
                    self.assertEqual({e["attempts"][0]["settled_reason"] for e in sections.values()}, {expected})
                    self.assertFalse((base / "data").exists())

    def test_insight_monthly_summary_skip_is_unchanged_with_real_registry(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            (base / "ops").mkdir()
            (base / "ops" / "legacy_weeks.json").write_text(
                (ROOT / "ops" / "legacy_weeks.json").read_text(encoding="utf-8"), encoding="utf-8")
            code, out = self.run_main(run_insight, base, self.insight_payloads(), period=W4_SUMMARY)
            self.assertEqual(code, 0)  # 정상적인 월간 종합 건너뛰기는 종료 코드 0, 상태 기록 없음
            self.assertEqual(self.last_collect_calls, [])
            self.assertFalse((base / "ops" / "status").exists())
            self.assertFalse((base / "logs").exists())

    def test_unapproved_registry_entry_does_not_block(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            self.registry(base, approved=False)
            code, _ = self.run_main(run_lifestyle, base, self.lifestyle_payloads(architecture=cat([item(TITLE_A, "https://a.com/1")])))
            self.assertEqual(code, 0)
            self.assertEqual(len(self.last_collect_calls), 1)
            self.assertEqual(self.status(base)["sections"]["architecture"]["effective"]["status"], "saved")

    def test_corrupt_registry_stops_before_collection(self):
        for module in (run_insight, run_lifestyle):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as t:
                base = Path(t)
                (base / "ops").mkdir()
                (base / "ops" / "legacy_weeks.json").write_text("{bad", encoding="utf-8")
                code, out = self.run_main(module, base, {})
                self.assertEqual(code, 1)
                self.assertIn("STATUS_READ_FAILED", out)
                self.assertEqual(self.last_collect_calls, [])
                self.assertFalse((base / "data").exists())

    def test_guard_leaves_real_repository_legacy_data_untouched(self):
        # DATA_DIR 를 실제 저장소 data/ 로 두고 레지스트리·ops·logs 만 임시 폴더에 둔다. 가드가 깨지면 수집·필터·병합·인덱스
        # 갱신이 호출되는데 그 함수들은 AssertionError 를 내도록 막아 두므로, 실제 데이터는 어떤 경우에도 쓰이지 않는다.
        data_dir = ROOT / "data"

        def snapshot():
            return {str(p.relative_to(data_dir)): sha(p) for p in sorted(data_dir.rglob("*")) if p.is_file()}

        def forbidden(*args, **kwargs):
            raise AssertionError("legacy guard failed: a write/collect path was reached")

        before = snapshot()
        self.assertEqual(len([n for n in before if n.startswith("2026-09-W2_")]), 4)
        for module, keys, collect_name in ((run_insight, SECTION_KEYS, "collect_insight"),
                                           (run_lifestyle, LIFESTYLE_KEYS, "collect_lifestyle")):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as t:
                base = Path(t)
                self.registry(base, week="2026-09-W2")
                with contextlib.ExitStack() as st:
                    st.enter_context(mock.patch.object(module, "BASE_DIR", base))
                    st.enter_context(mock.patch.object(module, "DATA_DIR", data_dir))
                    st.enter_context(mock.patch.object(run_lifestyle, "INDEX_PATH", data_dir / "insight_index.json"))
                    st.enter_context(mock.patch.object(run_lifestyle, "_update_insight_index", forbidden))
                    for name in (collect_name, "merge_category_file", "filter_items"):
                        st.enter_context(mock.patch.object(module, name, forbidden))
                    st.enter_context(mock.patch.object(module, "get_last_completed_week_period", lambda: W2_LEGACY))
                    st.enter_context(mock.patch.object(module, "prepare_backend",
                                                       lambda log=print: (FakeBackend(), None)))
                    with contextlib.redirect_stdout(io.StringIO()):
                        code = module.main()
                self.assertEqual(code, 1)
                sections = ops_state.read_week(base / "ops", "2026-09-W2")["sections"]
                self.assertEqual(sorted(sections), sorted(keys))
                self.assertEqual({s["effective"]["settled_reason"] for s in sections.values()},
                                 {"legacy_label_conflict"})
                self.assertFalse((base / "data").exists())
        self.assertEqual(snapshot(), before)  # 실제 저장소 data/ 전체(파일명·해시) 불변


class NoneBackendWritesNothingTests(unittest.TestCase):
    def test_unset_backend_creates_no_status_logs_or_data(self):
        for module in (run_insight, run_lifestyle):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as t:
                base = Path(t)
                with contextlib.ExitStack() as st:
                    for target in {module, run_lifestyle}:
                        st.enter_context(mock.patch.object(target, "BASE_DIR", base))
                        st.enter_context(mock.patch.object(target, "DATA_DIR", base / "data"))
                    st.enter_context(mock.patch.object(backends, "load_env", lambda: False))
                    st.enter_context(mock.patch.dict(os.environ, {}))
                    os.environ.pop("AI_BACKEND", None)
                    with contextlib.redirect_stdout(io.StringIO()) as out:
                        code = module.main()
                self.assertEqual(code, 0)
                self.assertIn("STATUS=SKIPPED", out.getvalue())
                self.assertEqual(sorted(p.name for p in base.rglob("*")), [])


class PublicSafetyIntegrationTests(Harness):
    def test_generated_status_has_no_titles_urls_keys_or_paths(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            payloads = self.lifestyle_payloads(architecture=cat([item(TITLE_A, "https://a.com/1")]),
                                               culture=cat([item(TITLE_B, "https://a.com/2")]))
            self.run_main(run_lifestyle, base, payloads, failures={"wellness": "RuntimeError: https://x.com sk-ant-" + "aaaaaaaaaaaa"})
            text = ops_state.status_path(base / "ops", "2026-09-W3").read_text(encoding="utf-8")
            for needle in (TITLE_A, TITLE_B, "http", "a.com", "ak1:", "tk1:", "akey", "article_key", "sk-ant",
                           str(base), "Users"):
                self.assertNotIn(needle, text)
            self.assertEqual(ops_state.mask_secrets(text, {}), text)
            self.assertNotIn("\r", text)


EXIT_CODE_SCRIPT = textwrap.dedent("""
    import contextlib, io, sys
    from datetime import date
    from pathlib import Path
    from unittest import mock
    sys.path.insert(0, {root!r})
    import ops_state, run_insight, run_lifestyle
    from dates import WeekPeriod
    name, base, mode = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    module = run_insight if name == "insight" else run_lifestyle
    key = "trend" if name == "insight" else "architecture"
    collect_name = "collect_insight" if name == "insight" else "collect_lifestyle"
    period = WeekPeriod(year=2026, week_label="2026-09-W3", start=date(2026, 9, 21), end=date(2026, 9, 27))
    item = {{"title": "프로세스 종료 코드 확인용 기사 제목", "summary": "s", "source": "m", "source_url": "", "date": "2026-09-23"}}

    def fake_collect(p, fails, *, backend):
        return {{key: {{"range_label": "r", "domestic": [item], "global": []}}}}

    with contextlib.ExitStack() as st:
        for target in (module, run_lifestyle):
            st.enter_context(mock.patch.object(target, "BASE_DIR", base))
            st.enter_context(mock.patch.object(target, "DATA_DIR", base / "data"))
        st.enter_context(mock.patch.object(run_lifestyle, "INDEX_PATH", base / "data" / "insight_index.json"))
        st.enter_context(mock.patch.object(module, "get_last_completed_week_period", lambda: period))
        st.enter_context(mock.patch.object(module, "prepare_backend", lambda log=print: (object(), None)))
        st.enter_context(mock.patch.object(module, collect_name, fake_collect))
        if mode == "fail":
            st.enter_context(mock.patch.object(ops_state, "write_json_atomic",
                                               side_effect=ops_state.StatusWriteError("write_failed:OSError")))
        code = module.main()
    sys.exit(code)  # run_*.py 의 `if __name__ == "__main__": sys.exit(main())` 와 같은 방식
""")


class ExitCodeTests(Harness):
    """상태 기록 실패 -> 종료 코드 1, 그 외 정상 -> 0. 두 진입점 모두 같은 규칙이어야 한다."""

    CASES = (("insight", run_insight, "trend"), ("lifestyle", run_lifestyle, "architecture"))

    def payloads(self, module, key):
        make = self.insight_payloads if module is run_insight else self.lifestyle_payloads
        return make(**{key: cat([item(TITLE_A, "https://a.com/1")])})

    def data_file(self, base, key):
        return base / "data" / f"2026-09-W3_{key}.json"

    def test_normal_run_returns_0_for_both_entry_points(self):
        for name, module, key in self.CASES:
            with self.subTest(module=name), tempfile.TemporaryDirectory() as t:
                base = Path(t)
                code, out = self.run_main(module, base, self.payloads(module, key))
                self.assertEqual(code, 0)
                self.assertNotIn("STATUS_WRITE_FAILED", out)
                self.assertEqual(self.status(base)["sections"][key]["effective"]["status"], "saved")

    def test_status_write_failure_returns_1_for_both_entry_points(self):
        for name, module, key in self.CASES:
            with self.subTest(module=name), tempfile.TemporaryDirectory() as t:
                base = Path(t)
                failing = mock.patch.object(ops_state, "write_json_atomic",
                                            side_effect=ops_state.StatusWriteError("write_failed:OSError"))
                code, out = self.run_main(module, base, self.payloads(module, key), extra=[failing])
                self.assertEqual(code, 1)
                self.assertIn("STATUS_WRITE_FAILED", out)
                self.assertTrue(self.data_file(base, key).exists())  # 데이터는 이미 저장됨
                state = ops_state.section_state(base / "ops", "2026-09-W3", key)
                self.assertEqual((state["status"], state["settled_reason"]), ("unprocessed", "not_run"))

    def test_lock_timeout_during_status_record_returns_1_for_both_entry_points(self):
        @contextlib.contextmanager
        def timed_out(*args, **kwargs):
            raise ops_state.StatusWriteError("lock_timeout")
            yield  # pragma: no cover

        for name, module, key in self.CASES:
            with self.subTest(module=name), tempfile.TemporaryDirectory() as t:
                base = Path(t)
                code, out = self.run_main(module, base, self.payloads(module, key),
                                          extra=[mock.patch.object(ops_state, "week_lock", timed_out)])
                self.assertEqual(code, 1)
                self.assertIn("STATUS_WRITE_FAILED: lock_timeout", out)
                self.assertTrue(self.data_file(base, key).exists())

    def test_unreadable_existing_status_file_returns_1_and_is_not_overwritten(self):
        for name, module, key in self.CASES:
            with self.subTest(module=name), tempfile.TemporaryDirectory() as t:
                base = Path(t)
                status = ops_state.status_path(base / "ops", "2026-09-W3")
                status.parent.mkdir(parents=True)
                status.write_text("{corrupt", encoding="utf-8")
                code, out = self.run_main(module, base, self.payloads(module, key))
                self.assertEqual(code, 1)
                self.assertIn("STATUS_WRITE_FAILED", out)
                self.assertEqual(status.read_text(encoding="utf-8"), "{corrupt")
                self.assertTrue(self.data_file(base, key).exists())

    def test_collect_failures_without_status_failure_still_return_0(self):
        # 상태 기록 실패 외의 종료 코드는 바뀌지 않는다: 수집 실패는 상태에 failed 로 기록되고 종료 코드는 0.
        for name, module, key in self.CASES:
            with self.subTest(module=name), tempfile.TemporaryDirectory() as t:
                base = Path(t)
                code, out = self.run_main(module, base, self.payloads(module, key),
                                          failures={key: "RuntimeError: api down"})
                self.assertEqual(code, 0)
                self.assertIn(f"FAILED {key}", out)
                self.assertEqual(self.status(base)["sections"][key]["effective"]["status"], "failed")

    def test_legacy_guard_status_write_failure_still_returns_1(self):
        for name, module, key in self.CASES:
            with self.subTest(module=name), tempfile.TemporaryDirectory() as t:
                base = Path(t)
                (base / "ops").mkdir()
                (base / "ops" / "legacy_weeks.json").write_text(json.dumps(
                    {"schema_version": 1, "weeks": {"2026-09-W3": {"reason": "legacy_label_conflict", "approved": True}}}),
                    encoding="utf-8")
                failing = mock.patch.object(ops_state, "write_json_atomic",
                                            side_effect=ops_state.StatusWriteError("write_failed:OSError"))
                code, out = self.run_main(module, base, self.payloads(module, key), extra=[failing])
                self.assertEqual(code, 1)
                self.assertIn("STATUS_WRITE_FAILED", out)
                self.assertEqual(self.last_collect_calls, [])

    def test_process_exit_status_matches_main_return_value(self):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        script = EXIT_CODE_SCRIPT.format(root=str(ROOT))
        for name, module, key in self.CASES:
            for mode, expected in (("ok", 0), ("fail", 1)):
                with self.subTest(module=name, mode=mode), tempfile.TemporaryDirectory() as t:
                    proc = subprocess.run([sys.executable, "-B", "-c", script, name, t, mode], capture_output=True,
                                          text=True, encoding="utf-8", errors="replace", env=env, cwd=str(ROOT),
                                          timeout=120)
                    self.assertEqual(proc.returncode, expected, proc.stderr[-300:])
                    self.assertEqual("STATUS_WRITE_FAILED" in proc.stdout, mode == "fail")
                    self.assertTrue((Path(t) / "data" / f"2026-09-W3_{key}.json").exists())


if __name__ == "__main__":
    unittest.main()

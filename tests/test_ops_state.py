"""ops_state 단위 테스트: 판정표, effective 병합, 집계, 원자적 쓰기, 락, 레거시 레지스트리, 공개 안전성."""

import itertools
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import ops_state as ops
from dates import WeekPeriod
from ops_state import (
    Classification,
    SectionOutcome,
    StatusReadError,
    StatusWriteError,
    classify,
    make_unit,
)

ROOT = Path(__file__).resolve().parent.parent
W3 = WeekPeriod(year=2026, week_label="2026-09-W3", start=date(2026, 9, 21), end=date(2026, 9, 27))
RUN_AT = "2026-11-02T09:00:00+09:00"


def unit(ok=True, role="active", kind="ai_backend", uid="ai_backend:test"):
    return make_unit(uid, kind, ok, role)


class ClassifyTableTests(unittest.TestCase):
    def test_all_rows(self):
        cases = [
            # (units, saved_total, kwargs, 기대)
            ([], 5, {}, ("unprocessed", False, "no_enabled_source")),
            ([unit(role="check_only")], 5, {}, ("unprocessed", False, "no_enabled_source")),
            ([unit(False)], None, {}, ("failed", False, "all_sources_failed")),
            ([unit(False), unit(False)], 3, {}, ("failed", False, "all_sources_failed")),
            ([unit(True)], 3, {}, ("saved", True, "all_sources_ok")),
            ([unit(True), unit(True)], 1, {}, ("saved", True, "all_sources_ok")),
            ([unit(True)], 0, {}, ("empty", True, "all_sources_ok")),
            ([unit(True), unit(False)], 3, {}, ("saved", False, "partial_source_failure")),
            ([unit(True), unit(False)], 0, {}, ("empty", False, "partial_source_failure")),
            ([unit(True)], 3, {"processing_error": True}, ("failed", False, "processing_error")),
            ([unit(True)], 3, {"guard": "legacy_label_conflict"}, ("failed", False, "legacy_label_conflict")),
            ([unit(True)], 3, {"guard": "legacy_aggregate"}, ("failed", False, "legacy_aggregate")),
            ([], None, {"guard": "legacy_aggregate"}, ("failed", False, "legacy_aggregate")),
            ([unit(True)], 3, {"guard": "unknown_reason"}, ("failed", False, "legacy_label_conflict")),  # 알 수 없는 사유는 기본값
            ([unit(True)], 3, {"guard": True}, ("failed", False, "legacy_label_conflict")),
        ]
        for units, total, kwargs, expected in cases:
            with self.subTest(units=[(u["ok"], u["role"]) for u in units], total=total, kwargs=kwargs):
                got = classify(units, total, **kwargs)
                self.assertEqual((got.status, got.settled, got.settled_reason), expected)

    def test_priority_guard_over_processing_over_units(self):
        failed = [unit(False)]
        self.assertEqual(classify(failed, 3, processing_error=True, guard="x").settled_reason, "legacy_label_conflict")
        self.assertEqual(classify(failed, 3, processing_error=True, guard="legacy_aggregate").settled_reason, "legacy_aggregate")
        self.assertEqual(classify(failed, 3, processing_error=True).settled_reason, "processing_error")
        self.assertEqual(classify([], 3, processing_error=True).settled_reason, "processing_error")
        self.assertEqual(classify([], 3, guard="x").settled_reason, "legacy_label_conflict")

    def test_check_only_units_are_excluded_from_decision(self):
        self.assertEqual(classify([unit(True), unit(False, "check_only")], 2).settled, True)
        self.assertEqual(classify([unit(False), unit(True, "check_only")], 2).status, "failed")
        self.assertEqual(classify([unit(True), unit(True), unit(False, "check_only")], 0).status, "empty")

    def test_settled_invariant_over_exhaustive_inputs(self):
        for n_active, n_failed, total, proc, guard, n_check in itertools.product(
                range(4), range(4), (0, 1, 5), (False, True), (None, "g"), (0, 2)):
            if n_failed > n_active:
                continue
            units = [unit(i >= n_failed) for i in range(n_active)] + [unit(False, "check_only")] * n_check
            got = classify(units, total, processing_error=proc, guard=guard)
            if got.settled:
                self.assertGreaterEqual(n_active, 1)
                self.assertEqual(n_failed, 0)
                self.assertIn(got.status, ("saved", "empty"))
                self.assertFalse(proc or guard)
            self.assertIn(got.status, ops.STATUSES)
            self.assertIn(got.settled_reason, ops.REASONS)
            if got.status == "unprocessed":
                self.assertFalse(got.settled)

    def test_classification_is_frozen_value(self):
        self.assertEqual(classify([unit()], 1), Classification("saved", True, "all_sources_ok"))


class UnitTests(unittest.TestCase):
    def test_make_unit_validation_and_sanitizing(self):
        with self.assertRaises(ValueError):
            make_unit("x", "bogus_kind", True)
        with self.assertRaises(ValueError):
            make_unit("x", "ai_backend", True, role="bogus")
        u = make_unit("ai_backend:anthropic", "ai_backend", False, "active", "RuntimeError", "not_a_code")
        self.assertEqual(u["error_code"], "unknown")
        u = make_unit("ai_backend:bad name with spaces and http://x.com", "ai_backend", True)
        self.assertEqual(u["id"], "unknown")
        self.assertIsNone(make_unit("a", "ai_backend", True)["error_type"])

    def test_units_summary_tracks_article_source_only_when_present(self):
        summary = ops.units_summary([unit(True), unit(False, "check_only")])
        self.assertEqual(summary["ai_backend"], {"tracked": True, "active": 1, "check_only": 1, "ok": 1, "failed": 0})
        self.assertEqual(summary["article_source"], {"tracked": False})
        with_sources = ops.units_summary([unit(True), unit(True, kind="article_source", uid="article_source:a"),
                                          unit(False, kind="article_source", uid="article_source:b")])
        self.assertEqual(with_sources["article_source"],
                         {"tracked": True, "active": 2, "check_only": 0, "ok": 1, "failed": 1})

    def test_article_sources_extend_without_schema_change(self):
        units = [unit(True), unit(True, kind="article_source", uid="article_source:a"),
                 unit(False, kind="article_source", uid="article_source:b")]
        got = classify(units, 2)
        self.assertEqual((got.status, got.settled, got.settled_reason), ("saved", False, "partial_source_failure"))


class SafeTextTests(unittest.TestCase):
    def test_masks_urls_paths_secrets_and_limits_length(self):
        fake_key = "sk-ant-" + "api03-ABCDEFGHIJKLMNOP1234567890"
        text = f"failed https://a.com/x?token=abc key {fake_key} path C:" + "\\Users\\Someone\\f.py " + "z" * 300
        out = ops.safe_text(text)
        for needle in ("https://", "a.com", fake_key, "Someone"):
            self.assertNotIn(needle, out)
        self.assertLessEqual(len(out), 100)

    def test_error_type_of_keeps_only_class_name(self):
        self.assertEqual(ops.error_type_of("RuntimeError: boom https://x.com secret"), "RuntimeError")
        self.assertEqual(ops.error_type_of(""), "UnknownError")
        self.assertEqual(ops.error_type_of(None), "UnknownError")
        self.assertEqual(ops.error_type_of("weird class name: x"), "UnknownError")

    def test_run_id_format(self):
        rid = ops.new_run_id("insight")
        self.assertRegex(rid, r"^\d{8}T\d{6}-insight-[0-9a-f]{6}$")
        self.assertNotEqual(rid, ops.new_run_id("insight"))


class SummarizeLogTests(unittest.TestCase):
    def test_counts_by_category(self):
        log = [
            {"category": "a", "action": "excluded", "reason": "date_missing", "title": "T1"},
            {"category": "a", "action": "excluded", "reason": "date_out_of_range"},
            {"category": "a", "action": "excluded", "reason": "date_low_confidence"},
            {"category": "a", "action": "excluded", "reason": "duplicate_url"},
            {"category": "a", "action": "excluded", "reason": "something_new"},
            {"category": "a", "action": "shortfall", "kept": 0},
            {"category": "a", "action": "url_check", "url_status": "invalid"},
            {"category": "a", "action": "url_check", "url_status": "unverified"},
            {"category": "a", "action": "shadow", "reason": "l1_only_match"},
            {"category": "a", "action": "multi_classified", "also_in": ["b"]},
            {"category": "a", "action": "kept", "reason": "similar_title_kept"},
            {"category": "b", "action": "excluded", "reason": "date_missing"},
        ]
        a = ops.summarize_log(log, "a")
        self.assertEqual(a["excluded"]["date_missing"], 1)
        self.assertEqual(a["excluded"]["date_out_of_range"], 1)
        self.assertEqual(a["excluded"]["date_low_confidence"], 1)
        self.assertEqual(a["excluded"]["duplicate_url"], 1)
        self.assertEqual(a["excluded"]["other"], 1)
        self.assertEqual((a["shortfall"], a["shadow_l1_only"], a["multi_classified"]), (1, 1, 1))
        self.assertEqual(a["url_check"], {"invalid": 1, "unverified": 1})
        self.assertEqual(ops.summarize_log(log, "b")["excluded"]["date_missing"], 1)
        self.assertEqual(ops.summarize_log(log, "zzz")["excluded"]["date_missing"], 0)


class BuildAttemptTests(unittest.TestCase):
    def test_success_failure_and_guard_shapes(self):
        ok = ops.build_attempt("r1", RUN_AT, "lifestyle", SectionOutcome("fake", saved_new=2, saved_total=5), [], "x")
        self.assertEqual((ok["status"], ok["settled"], ok["settled_reason"]), ("saved", True, "all_sources_ok"))
        self.assertEqual((ok["counts"]["saved_new"], ok["counts"]["saved_total"]), (2, 5))
        self.assertEqual(ok["units"][0]["id"], "ai_backend:fake")
        self.assertEqual(ok["units_summary"]["article_source"], {"tracked": False})
        empty = ops.build_attempt("r1", RUN_AT, "insight", SectionOutcome("fake", saved_new=0, saved_total=0), [], "x")
        self.assertEqual((empty["status"], empty["settled"]), ("empty", True))
        collect = ops.build_attempt("r1", RUN_AT, "insight", SectionOutcome("fake", collect_error="RuntimeError"), [], "x")
        self.assertEqual((collect["status"], collect["settled_reason"]), ("failed", "all_sources_failed"))
        self.assertIsNone(collect["counts"]["saved_total"])
        self.assertEqual(collect["units"][0]["error_code"], "collect_failed")
        proc = ops.build_attempt("r1", RUN_AT, "insight", SectionOutcome("fake", processing_error="KeyError", saved_new=3, saved_total=3), [], "x")
        self.assertEqual((proc["status"], proc["settled_reason"]), ("failed", "processing_error"))
        self.assertIsNone(proc["counts"]["saved_total"])
        self.assertEqual(proc["counts"]["saved_new"], 0)
        guard = ops.build_attempt("r1", RUN_AT, "insight", SectionOutcome("fake", guard="legacy_aggregate"), [], "x")
        self.assertEqual((guard["status"], guard["settled_reason"], guard["units"]), ("failed", "legacy_aggregate", []))
        guard2 = ops.build_attempt("r1", RUN_AT, "insight", SectionOutcome("fake", guard="legacy_label_conflict"), [], "x")
        self.assertEqual((guard2["status"], guard2["settled_reason"], guard2["units"]), ("failed", "legacy_label_conflict", []))
        self.assertFalse(guard["settled"] or guard2["settled"])

    def test_invalid_kind_rejected(self):
        with self.assertRaises(ValueError):
            ops.build_attempt("r", RUN_AT, "monthly", SectionOutcome("fake"), [], "x")


def att(status, settled, reason="all_sources_ok", rid="r"):
    return {"run_id": rid, "run_at": RUN_AT, "kind": "insight", "status": status, "settled": settled,
            "settled_reason": reason}


class EffectiveTests(unittest.TestCase):
    def eff(self, attempts):
        best = ops.pick_effective(attempts)
        return None if best is None else (best["run_id"], best["status"], best["settled"])

    def test_priority_settled_then_saved_empty_then_failed(self):
        self.assertIsNone(self.eff([]))
        self.assertEqual(self.eff([att("failed", False, "all_sources_failed", "a")]), ("a", "failed", False))
        self.assertEqual(self.eff([att("failed", False, "all_sources_failed", "a"),
                                   att("saved", False, "partial_source_failure", "b")]), ("b", "saved", False))
        self.assertEqual(self.eff([att("saved", False, "partial_source_failure", "a"),
                                   att("failed", False, "all_sources_failed", "b")]), ("a", "saved", False))
        self.assertEqual(self.eff([att("unprocessed", False, "no_enabled_source", "a"),
                                   att("failed", False, "all_sources_failed", "b")]), ("b", "failed", False))

    def test_settled_never_regresses_but_resettle_updates(self):
        base = [att("saved", True, rid="a")]
        for later in (att("failed", False, "all_sources_failed", "b"),
                      att("saved", False, "partial_source_failure", "b"),
                      att("failed", False, "processing_error", "b"),
                      att("failed", False, "legacy_label_conflict", "b"),
                      att("unprocessed", False, "no_enabled_source", "b")):
            with self.subTest(later=later["settled_reason"]):
                self.assertEqual(self.eff(base + [later]), ("a", "saved", True))
        self.assertEqual(self.eff([att("empty", True, rid="a"), att("saved", True, rid="b")]), ("b", "saved", True))
        self.assertEqual(self.eff([att("saved", True, rid="a"), att("failed", False, "all_sources_failed", "b"),
                                   att("saved", True, rid="c")]), ("c", "saved", True))

    def test_trim_keeps_recent_and_effective(self):
        attempts = [att("saved", True, rid="old")] + [att("failed", False, "all_sources_failed", f"f{i}") for i in range(30)]
        best = ops.pick_effective(attempts)
        trimmed = ops.trim_attempts(attempts, best["run_id"])
        self.assertEqual(len(trimmed), 21)  # 최근 20건 + effective
        self.assertEqual(trimmed[0]["run_id"], "old")
        self.assertEqual(trimmed[-1]["run_id"], "f29")
        short = attempts[:5]
        self.assertEqual(ops.trim_attempts(short, "x"), short)

    def test_unprocessed_state_default(self):
        state = ops.unprocessed_state()
        self.assertEqual((state["status"], state["settled"], state["settled_reason"]), ("unprocessed", False, "not_run"))


class AtomicWriteTests(unittest.TestCase):
    def test_write_creates_file_lf_and_leaves_no_temp(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "status" / "w.json"
            ops.write_json_atomic(path, {"한글": 1})
            raw = path.read_bytes()
            self.assertTrue(raw.endswith(b"\n"))
            self.assertNotIn(b"\r", raw)
            self.assertEqual(json.loads(raw.decode("utf-8")), {"한글": 1})
            self.assertEqual([p.name for p in path.parent.iterdir()], ["w.json"])

    def test_failure_preserves_existing_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "w.json"
            ops.write_json_atomic(path, {"v": 1})
            before = path.read_bytes()
            with self.assertRaises(StatusWriteError):
                ops.write_json_atomic(path, {"v": {1, 2}})  # 직렬화 불가
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual([p.name for p in Path(t).iterdir()], ["w.json"])

    def test_replace_failure_preserves_existing_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "w.json"
            ops.write_json_atomic(path, {"v": 1})
            before = path.read_bytes()
            with mock.patch.object(ops.os, "replace", side_effect=PermissionError("locked")), \
                    mock.patch.object(ops.time, "sleep", lambda s: None):
                with self.assertRaises(StatusWriteError):
                    ops.write_json_atomic(path, {"v": 2})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual([p.name for p in Path(t).iterdir()], ["w.json"])

    def test_temp_file_is_in_same_directory(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "w.json"
            seen = []
            real_replace = os.replace

            def spy(src, dst):
                seen.append((Path(src).parent, Path(dst).parent))
                return real_replace(src, dst)

            with mock.patch.object(ops.os, "replace", spy):
                ops.write_json_atomic(path, {"v": 1})
            self.assertEqual(seen[0][0], seen[0][1])


class ReadTests(unittest.TestCase):
    def test_missing_file_and_section_are_unprocessed(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertIsNone(ops.read_week(Path(t), "2026-09-W3"))
            state = ops.section_state(Path(t), "2026-09-W3", "trend")
            self.assertEqual((state["status"], state["settled"], state["settled_reason"]), ("unprocessed", False, "not_run"))
            self.assertEqual(list(Path(t).rglob("*")), [])  # 읽기만으로는 파일을 만들지 않는다(백필 없음)

    def test_missing_section_in_existing_file(self):
        with tempfile.TemporaryDirectory() as t:
            ops.record_run(Path(t), W3, "insight", "r1", RUN_AT, {"trend": SectionOutcome("fake", saved_new=1, saved_total=1)}, [])
            self.assertEqual(ops.section_state(Path(t) / "ops", "2026-09-W3", "trend")["status"], "saved")
            self.assertEqual(ops.section_state(Path(t) / "ops", "2026-09-W3", "amenity")["settled_reason"], "not_run")

    def test_corrupt_or_unknown_schema_raises(self):
        with tempfile.TemporaryDirectory() as t:
            path = ops.status_path(Path(t), "2026-09-W3")
            path.parent.mkdir(parents=True)
            for content in ("{not json", json.dumps({"schema_version": 99, "sections": {}}), json.dumps([1])):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(StatusReadError):
                    ops.read_week(Path(t), "2026-09-W3")


class LegacyRegistryTests(unittest.TestCase):
    def test_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(ops.load_legacy_registry(Path(t) / "legacy_weeks.json"), {})

    def test_only_approved_entries(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "legacy_weeks.json"
            path.write_text(json.dumps({"schema_version": 1, "weeks": {
                "2026-09-W2": {"reason": "legacy_label_conflict", "approved": True},
                "2026-08-W4": {"reason": "legacy_aggregate", "approved": False},
                "2026-07-W1": {"reason": "x"},
                "bad-label": {"reason": "x", "approved": True},
            }}), encoding="utf-8")
            self.assertEqual(ops.load_legacy_registry(path), {"2026-09-W2": {"reason": "legacy_label_conflict"}})

    def test_corrupt_registry_fails_closed(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "legacy_weeks.json"
            for content in ("{bad", json.dumps({"weeks": []}), json.dumps({})):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(StatusReadError):
                    ops.load_legacy_registry(path)

    def test_repository_registry_contents(self):
        registry = ops.load_legacy_registry(ROOT / "ops" / "legacy_weeks.json")
        self.assertEqual(registry, {"2026-08-W4": {"reason": "legacy_aggregate"},
                                    "2026-09-W2": {"reason": "legacy_label_conflict"}})
        self.assertNotIn("2026-09-W3", registry)


class RecordRunTests(unittest.TestCase):
    def outcomes(self, keys, **kw):
        return {k: SectionOutcome("fake", saved_new=1, saved_total=1, **kw) for k in keys}

    def test_insight_and_lifestyle_runs_do_not_overwrite_each_other(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            ops.record_run(base, W3, "insight", "r1", RUN_AT, self.outcomes(["trend", "amenity"]), [])
            ops.record_run(base, W3, "lifestyle", "r2", RUN_AT, self.outcomes(["architecture"]), [])
            data = ops.read_week(base / "ops", "2026-09-W3")
            self.assertEqual(sorted(data["sections"]), ["amenity", "architecture", "trend"])
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual(data["range"], {"start": "2026-09-21", "end": "2026-09-27"})
            self.assertEqual(data["sections"]["architecture"]["attempts"][0]["kind"], "lifestyle")

    def test_attempts_accumulate_and_effective_does_not_regress(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            ops.record_run(base, W3, "insight", "r1", RUN_AT, self.outcomes(["trend"]), [])
            ops.record_run(base, W3, "insight", "r2", RUN_AT, {"trend": SectionOutcome("fake", collect_error="RuntimeError")}, [])
            ops.record_run(base, W3, "insight", "r3", RUN_AT, {"trend": SectionOutcome("fake", processing_error="KeyError")}, [])
            entry = ops.read_week(base / "ops", "2026-09-W3")["sections"]["trend"]
            self.assertEqual([a["run_id"] for a in entry["attempts"]], ["r1", "r2", "r3"])
            self.assertEqual([a["status"] for a in entry["attempts"]], ["saved", "failed", "failed"])
            self.assertEqual(entry["effective"]["run_id"], "r1")
            self.assertTrue(entry["effective"]["settled"])
            ops.record_run(base, W3, "insight", "r4", RUN_AT, {"trend": SectionOutcome("fake", saved_new=0, saved_total=1)}, [])
            self.assertEqual(ops.read_week(base / "ops", "2026-09-W3")["sections"]["trend"]["effective"]["run_id"], "r4")

    def test_attempt_limit_applies(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            ops.record_run(base, W3, "insight", "first", RUN_AT, self.outcomes(["trend"]), [])
            for i in range(25):
                ops.record_run(base, W3, "insight", f"f{i}", RUN_AT, {"trend": SectionOutcome("fake", collect_error="E")}, [])
            entry = ops.read_week(base / "ops", "2026-09-W3")["sections"]["trend"]
            self.assertEqual(len(entry["attempts"]), 21)
            self.assertEqual(entry["attempts"][0]["run_id"], "first")
            self.assertEqual(entry["effective"]["run_id"], "first")

    def test_paths_follow_base_dir_argument(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            ops.record_run(Path(a), W3, "insight", "r1", RUN_AT, self.outcomes(["trend"]), [])
            self.assertTrue((Path(a) / "ops" / "status" / "2026-09-W3.json").exists())
            self.assertFalse((Path(b) / "ops").exists())

    def test_concurrent_writers_do_not_lose_sections(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            errors = []

            def worker(kind, keys, rid):
                try:
                    for i in range(5):
                        ops.record_run(base, W3, kind, f"{rid}{i}", RUN_AT, self.outcomes(keys), [])
                except Exception as e:  # pragma: no cover - 실패 시 단언에서 드러남
                    errors.append(repr(e))

            threads = [threading.Thread(target=worker, args=("insight", ["trend", "amenity"], "a")),
                       threading.Thread(target=worker, args=("lifestyle", ["architecture", "culture"], "b"))]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            self.assertEqual(errors, [])
            sections = ops.read_week(base / "ops", "2026-09-W3")["sections"]
            self.assertEqual(sorted(sections), ["amenity", "architecture", "culture", "trend"])
            for entry in sections.values():
                self.assertEqual(len(entry["attempts"]), 5)

    def test_lock_timeout_raises_write_error_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            with ops.week_lock(base / "logs" / ".locks", "2026-09-W3"):
                with self.assertRaises(StatusWriteError):
                    ops.record_run(base, W3, "insight", "r1", RUN_AT, self.outcomes(["trend"]), [], lock_timeout=0.2)
            self.assertFalse((base / "ops").exists())

    def test_unreadable_existing_status_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            path = ops.status_path(base / "ops", "2026-09-W3")
            path.parent.mkdir(parents=True)
            path.write_text("{corrupt", encoding="utf-8")
            with self.assertRaises(StatusWriteError):
                ops.record_run(base, W3, "insight", "r1", RUN_AT, self.outcomes(["trend"]), [])
            self.assertEqual(path.read_text(encoding="utf-8"), "{corrupt")

    def test_persistent_lock_file_is_kept_and_released_after_record(self):
        with tempfile.TemporaryDirectory() as t:
            ops.record_run(Path(t), W3, "insight", "r1", RUN_AT, self.outcomes(["trend"]), [])
            locks = list((Path(t) / "logs" / ".locks").iterdir())
            self.assertEqual([p.name for p in locks], ["2026-09-W3.lock"])  # 영구 락 파일은 남는다(삭제하지 않는다)
            self.assertEqual(locks[0].stat().st_size, 0)  # 내용을 쓰지 않는다
            with ops.week_lock(Path(t) / "logs" / ".locks", "2026-09-W3", timeout=1):  # 잠금은 해제되어 있다
                pass


class PublicSafetyTests(unittest.TestCase):
    def test_assert_public_safe(self):
        ops.assert_public_safe(json.dumps({"status": "saved", "run_at": RUN_AT}))
        for bad in ('{"x": "see https://a.com"}', '{"k": "ak1:abc"}', '{"k": "tk1:abc"}', '{"akey": 1}',
                    '{"article_key": 1}', '{"p": "C:' + '\\\\Users\\\\x"}', '{"p": "/ho' + 'me/x"}',
                    '{"token": "abcd1234efgh"}'):
            with self.subTest(bad=bad):
                with self.assertRaises(StatusWriteError):
                    ops.assert_public_safe(bad)

    def test_assert_public_safe_blocks_url_forms_not_plain_words(self):
        for ok in ('{"error_type": "httpxError"}', '{"error_type": "HTTPError"}', '{"id": "ai_backend:httpclient"}'):
            with self.subTest(ok=ok):
                ops.assert_public_safe(ok)
        for bad in ('{"x": "http://a.com"}', '{"x": "HTTPS://A.COM/p"}', '{"x": "see https://a.com"}'):
            with self.subTest(bad=bad):
                with self.assertRaises(StatusWriteError):
                    ops.assert_public_safe(bad)

    def test_exception_class_name_containing_http_is_recorded(self):
        with tempfile.TemporaryDirectory() as t:
            ops.record_run(Path(t), W3, "insight", "r1", RUN_AT,
                           {"trend": SectionOutcome("fake", processing_error="httpxError")}, [])
            unit_ = ops.read_week(Path(t) / "ops", "2026-09-W3")["sections"]["trend"]["attempts"][0]["units"][0]
            self.assertEqual((unit_["error_type"], unit_["error_code"]), ("httpxError", "processing_error"))

    def test_status_file_has_no_titles_urls_keys_or_free_text(self):
        secret_title = "한남동 신규 레지던스 단독 소식"
        log = [{"category": "trend", "action": "excluded", "reason": "date_missing", "title": secret_title,
                "source_url": "https://news.example.com/a?idxno=1", "matched_title": secret_title},
               {"category": "trend", "action": "shadow", "reason": "l1_only_match", "key": "ak1:0123456789ab",
                "title": secret_title}]
        with tempfile.TemporaryDirectory() as t:
            ops.record_run(Path(t), W3, "insight", "r1", RUN_AT,
                           {"trend": SectionOutcome("fake", saved_new=1, saved_total=2),
                            "amenity": SectionOutcome("fake", collect_error="RuntimeError: https://x.com sk-ant-" + "zzzzzzzzzzzz"),
                            "facility": SectionOutcome("fake", processing_error="KeyError('title')")},
                           log)
            text = ops.status_path(Path(t) / "ops", "2026-09-W3").read_text(encoding="utf-8")
        for needle in (secret_title, "http", "news.example.com", "ak1:", "tk1:", "akey", "article_key",
                       "sk-ant", "C:\\", "/home/", "boom"):
            self.assertNotIn(needle, text)
        self.assertEqual(ops.mask_secrets(text, {}), text)
        data = json.loads(text)
        self.assertEqual(data["sections"]["trend"]["attempts"][0]["counts"]["excluded"]["date_missing"], 1)
        self.assertEqual(data["sections"]["trend"]["attempts"][0]["counts"]["shadow_l1_only"], 1)
        self.assertEqual(data["sections"]["amenity"]["attempts"][0]["units"][0]["error_type"], "UnknownError")


if __name__ == "__main__":
    unittest.main()

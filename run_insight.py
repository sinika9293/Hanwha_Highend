"""최신 인사이트 4개 섹션 주간 수집 — 수동 실행 엔트리포인트.

run_lifestyle.py 와 같은 흐름: KST 기준 직전 월~일 주간을 계산해 조사하고, 저장 전에
발행일·중복·URL을 검증한 뒤 data/{week}_{section}.json 에 병합 저장한다(기존 항목 보존).
월간 파이프라인(main.py/collect.py)과 월간 종합 주차 데이터는 건드리지 않는다.
"""

from __future__ import annotations

import sys
from pathlib import Path

from backends import mask_secrets, prepare_backend
import run_lifestyle
from collect_insight import FACILITY_GROUPS, SAVE_FIELDS, TITLE_KEY, collect_insight
from content_quality import (
    append_run_log,
    count_items,
    filter_items,
    count_shadow,
    load_section_index,
    merge_category_file,
    now_iso,
)
from dates import get_last_completed_week_period

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

# 월간 종합 데이터가 주간 라벨로 저장된 주차: 주간 자동화 대상·중복 비교 대상에서 제외한다.
MONTHLY_SUMMARY_WEEKS = frozenset({"2026-08-W4"})

# 기간 내 기사가 이보다 적어도 기준을 낮추거나 오래된 기사로 채우지 않고, 로그에만 남긴다.
TARGET_PER_REGION = 2


def _strip(section: str, items: list[dict]) -> list[dict]:
    """date 등 검증 전용 필드를 제거하고 기존 주간 JSON의 필드만 남긴다."""
    return [{f: it[f] for f in SAVE_FIELDS[section] if f in it} for it in items]


def _filter_region(section: str, bucket, period, index, region: str, log: list) -> object:
    title_key = TITLE_KEY[section]

    def run(items, label):
        kept = filter_items(
            items, period, index, section, label, log, title_key=title_key, url_dedup_new=False
        )
        return _strip(section, kept)

    if section == "facility":
        bucket = bucket if isinstance(bucket, dict) else {}
        return {g: run(bucket.get(g, []), f"{region}/{g}") for g in FACILITY_GROUPS}
    return run(bucket if isinstance(bucket, list) else [], region)


def main() -> int:
    backend, exit_code = prepare_backend()
    if backend is None:
        return exit_code

    period = get_last_completed_week_period()
    if period.week_label in MONTHLY_SUMMARY_WEEKS:
        print(f"{period.week_label}은(는) 월간 종합 주차라 주간 수집 대상이 아닙니다. 건너뜁니다.")
        return 0

    failures: dict[str, str] = {}
    sections = collect_insight(period, failures, backend=backend)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log: list[dict] = []
    saved: list[str] = []
    for key, payload in sections.items():
        try:
            index = load_section_index(DATA_DIR, key, period.week_label, MONTHLY_SUMMARY_WEEKS)
            kept = {
                region: _filter_region(key, payload.get(region), period, index, region, log)
                for region in ("domestic", "global")
            }
            for region, bucket in kept.items():
                n = count_items(bucket)
                if n < TARGET_PER_REGION:
                    log.append({"category": key, "region": region, "action": "shortfall",
                                "kept": n, "target": TARGET_PER_REGION})
            out_path = DATA_DIR / f"{period.week_label}_{key}.json"
            total = count_items(kept["domestic"]) + count_items(kept["global"])
            if total == 0 and not out_path.exists():
                log.append({"category": key, "action": "empty_result"})
                print(f"skip {out_path.name} (기간 내 신규 항목 없음)")
                continue
            merge_category_file(
                out_path, period.week_label, key,
                payload.get("range_label", period.range_label), kept["domestic"], kept["global"],
            )
            saved.append(key)
            print(f"saved {out_path.name} (+{count_items(kept['domestic'])} 국내, +{count_items(kept['global'])} 해외)")
        except Exception as e:
            failures[key] = f"{type(e).__name__}: {mask_secrets(e)}"

    if saved:
        run_lifestyle._update_insight_index(period, saved)
        print(f"updated insight_index.json ({period.week_label})")

    log_path = append_run_log(
        BASE_DIR / "logs",
        period.week_label,
        {"run_at": now_iso(), "kind": "insight", "saved": saved, "collect_failed": failures,
         "shadow_l1_only_matches": count_shadow(log), "items": log},
    )
    for key, reason in failures.items():
        print(f"FAILED {key}: {reason}")
    print(f"log: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

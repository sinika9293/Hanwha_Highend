"""럭셔리 라이프스타일 10개 카테고리 주간 수집 — 수동 실행 엔트리포인트.

main.py(월간 파이프라인)는 건드리지 않는다. 이 스크립트는 "가장 최근에 끝난
월요일~일요일" 주간을 자동 계산해 10개 카테고리를 조사하고, 카테고리당 1개의
주간 JSON 파일로 저장한 뒤 data/insight_index.json의 해당 주차 entry에
섹션 키를 추가 갱신한다(기존 4개 섹션 키는 보존).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from backends import mask_secrets, prepare_backend
from collect_lifestyle import collect_lifestyle
from content_quality import (
    append_run_log,
    count_shadow,
    filter_items,
    load_section_index,
    load_url_locations,
    merge_category_file,
    normalize_url,
    now_iso,
)
from dates import get_last_completed_week_period, today_kst
from lifestyle_schema import LIFESTYLE_KEYS

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
INDEX_PATH = DATA_DIR / "insight_index.json"


def _update_insight_index(period, keys=LIFESTYLE_KEYS) -> None:
    """insight_index.json의 해당 주차 entry를 찾아(없으면 생성) sections에
    저장에 성공한 라이프스타일 키를 추가한다. 기존 섹션 키는 보존한다."""
    if INDEX_PATH.exists():
        index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    else:
        index = {"generated_on": "", "default": "", "weeks": []}

    week_entry = next(
        (w for w in index["weeks"] if w.get("week") == period.week_label), None
    )
    if week_entry is None:
        week_no = period.week_label.rsplit("W", 1)[-1]
        week_entry = {
            "week": period.week_label,
            "year": period.year,
            "month": period.start.month,
            "label": f"{period.year}년 {period.start.month}월 {week_no}주",
            "range_label": period.range_label,
            "sections": [],
        }
        index["weeks"].insert(0, week_entry)

    existing_sections = set(week_entry.get("sections", []))
    for key in keys:
        if key not in existing_sections:
            week_entry.setdefault("sections", []).append(key)

    index["generated_on"] = today_kst().isoformat()
    index["default"] = period.week_label

    INDEX_PATH.write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    backend, exit_code = prepare_backend()
    if backend is None:
        return exit_code

    period = get_last_completed_week_period()
    failures: dict[str, str] = {}
    categories = collect_lifestyle(period, failures, backend=backend)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log: list[dict] = []
    saved: list[str] = []
    kept_records: list[tuple[str, str, str, str]] = []  # (카테고리, 지역, 제목, L0 URL)
    for key, payload in categories.items():
        try:
            # 같은 카테고리의 전체 기존 주간 데이터만 비교한다. 다른 카테고리·주거 섹션과는 중복을 허용하며,
            # 이번 실행에서 채택한 항목도 이 카테고리 색인에만 쌓인다(카테고리 순서와 무관).
            index = load_section_index(DATA_DIR, key, period.week_label, same_week_url_dedup=True)
            kept = {
                region: filter_items(payload.get(region, []), period, index, key, region, log)
                for region in ("domestic", "global")
            }
            out_path = DATA_DIR / f"{period.week_label}_{key}.json"
            merge_category_file(
                out_path,
                period.week_label,
                key,
                payload.get("range_label", period.range_label),
                kept["domestic"],
                kept["global"],
            )
            saved.append(key)
            for region, items in kept.items():
                for it in items:
                    kept_records.append((key, region, it.get("title", ""), normalize_url(it.get("source_url", ""))))
            print(f"saved {out_path.name} (+{len(kept['domestic'])} 국내, +{len(kept['global'])} 해외)")
        except Exception as e:
            failures[key] = f"{type(e).__name__}: {mask_secrets(e)}"

    if saved:
        _update_insight_index(period, saved)
        print(f"updated {INDEX_PATH.name} ({period.week_label})")
        # 같은 기사가 다른 카테고리·섹션에도 있으면 제외하지 않고 로그로만 남긴다(다중 분류 허용).
        locations = load_url_locations(DATA_DIR)
        for key, region, title, nurl in kept_records:
            others = locations.get(nurl, set()) - {key} if nurl else set()
            if others:
                log.append({"category": key, "region": region, "title": title,
                            "action": "multi_classified", "also_in": sorted(others)})

    log_path = append_run_log(
        BASE_DIR / "logs",
        period.week_label,
        {"run_at": now_iso(), "saved": saved, "collect_failed": failures,
         "shadow_l1_only_matches": count_shadow(log), "items": log},
    )
    for key, reason in failures.items():
        print(f"FAILED {key}: {reason}")
    print(f"log: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

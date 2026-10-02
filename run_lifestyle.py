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

from dotenv import load_dotenv

from collect_lifestyle import collect_lifestyle
from dates import get_last_completed_week_period
from lifestyle_schema import LIFESTYLE_KEYS

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
INDEX_PATH = DATA_DIR / "insight_index.json"


def _update_insight_index(period) -> None:
    """insight_index.json의 해당 주차 entry를 찾아(없으면 생성) sections에
    10개 라이프스타일 키를 추가한다. 기존 섹션 키는 보존한다."""
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
    for key in LIFESTYLE_KEYS:
        if key not in existing_sections:
            week_entry.setdefault("sections", []).append(key)

    from datetime import date

    index["generated_on"] = date.today().isoformat()
    index["default"] = period.week_label

    INDEX_PATH.write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> None:
    load_dotenv()
    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("환경변수 ANTHROPIC_API_KEY가 설정되어 있지 않습니다.")
        sys.exit(1)

    period = get_last_completed_week_period()
    categories = collect_lifestyle(period)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for key, payload in categories.items():
        out_path = DATA_DIR / f"{period.week_label}_{key}.json"
        data = {
            "week": period.week_label,
            "category": key,
            "range_label": payload.get("range_label", period.range_label),
            "domestic": payload.get("domestic", []),
            "global": payload.get("global", []),
        }
        out_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"saved {out_path.name}")

    _update_insight_index(period)
    print(f"updated {INDEX_PATH.name} ({period.week_label})")


if __name__ == "__main__":
    main()

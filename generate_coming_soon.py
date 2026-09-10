"""다음달 'coming soon' 플레이스홀더 JSON 자동 생성.

매월 1일 기준으로 실행하면 다음달 라벨의 data/{label}_monthly.json이 아직 없을 때
하이엔드 시설 Unsplash 이미지 1장을 무작위로 골라 최소 뼈대 JSON을 만든다.
이미 파일이 있으면(실제 리포트가 이미 생성됐거나 이전에 생성된 플레이스홀더가 있으면)
아무 것도 하지 않는다 — 실제 리포트 데이터를 덮어쓰지 않기 위함.

사용법:
    py generate_coming_soon.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from dates import get_next_month_period

DATA_DIR = Path(__file__).parent / "data"

HIGHEND_FACILITY_IMAGES = [
    "https://images.unsplash.com/photo-1773754532196-014342510e64?auto=format&fit=crop&w=1600&q=80",
    "https://images.unsplash.com/photo-1758775150929-ba29a2db855a?auto=format&fit=crop&w=1600&q=80",
    "https://images.unsplash.com/photo-1764938257230-26d3d589c212?auto=format&fit=crop&w=1600&q=80",
    "https://images.unsplash.com/photo-1506905925346-21bda4d32df4?auto=format&fit=crop&w=1600&q=80",
    "https://images.unsplash.com/photo-1610641818989-c2051b5e2cfd?auto=format&fit=crop&w=1600&q=80",
    "https://images.unsplash.com/photo-1571003123894-1f0594d2b5d9?auto=format&fit=crop&w=1600&q=80",
]


def main() -> None:
    period = get_next_month_period()
    target = DATA_DIR / f"{period.label}_monthly.json"

    if target.exists():
        print(f"이미 존재함: {target.name} (건너뜀)")
        return

    payload = {
        "month": period.label,
        "status": "coming_soon",
        "title": f"{period.year}년 {period.month}월 하이엔드 주거 트렌드 리포트",
        "release_date": period.start.isoformat(),
        "banner_image": random.choice(HIGHEND_FACILITY_IMAGES),
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"생성 완료: {target}")


if __name__ == "__main__":
    main()

"""하이엔드 주거 트렌드 리포트 자동 생성 — 실행 엔트리.

실행 시점이 속한 달의 '전월 1일~말일'을 대상 기간으로 자동 계산한다.
예) 9월에 실행 → 8월 1일~31일 기준 리포트.

사용법:
    py main.py

산출물 (현재 폴더에 생성):
    YYYY-MM_trend.json / _amenity.json / _facility.json / _global_case.json  (섹션별 원본 데이터)
    YYYY-MM_하이엔드주거_트렌드보고서.md / .xlsx  (4개 섹션을 합친 통합 리포트)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from collect import collect
from dates import get_previous_month_period
from report import render_excel, render_markdown
from schema import SECTION_KEYS

OUTPUT_DIR = Path(__file__).parent


def main() -> None:
    load_dotenv()

    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY가 설정되지 않았습니다.\n"
            "  1) .env.example 을 .env 로 복사\n"
            "  2) .env 파일에 실제 키 입력\n"
            "후 다시 실행하세요.",
            file=sys.stderr,
        )
        sys.exit(1)

    period = get_previous_month_period()
    print(f"[1/3] 대상 기간: {period.range_label} ({period.label})")

    print("[2/3] Claude 웹검색으로 섹션별 조사 및 구조화 중... (수 분 소요될 수 있음)")
    sections = collect(period)

    base_name = f"{period.label}_하이엔드주거_트렌드보고서"
    md_path = OUTPUT_DIR / f"{base_name}.md"
    xlsx_path = OUTPUT_DIR / f"{base_name}.xlsx"
    json_paths = {key: OUTPUT_DIR / f"{period.label}_{key}.json" for key in SECTION_KEYS}

    print(f"[3/3] 결과 저장 중: {md_path.name}, {xlsx_path.name}")
    md_path.write_text(render_markdown(sections, period), encoding="utf-8")
    render_excel(sections, period, xlsx_path)
    # 섹션별 원본 구조화 데이터도 남겨 웹페이지 탭 렌더링·재검증에 쓴다.
    for key in SECTION_KEYS:
        json_paths[key].write_text(
            json.dumps(sections[key], ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print("완료.")
    print(f"  - {md_path}")
    print(f"  - {xlsx_path}")
    for key in SECTION_KEYS:
        print(f"  - {json_paths[key]}")


if __name__ == "__main__":
    main()

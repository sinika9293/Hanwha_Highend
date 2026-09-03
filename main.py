"""하이엔드 주거 트렌드 리포트 자동 생성 — 실행 엔트리.

실행 시점이 속한 달의 '전월 1일~말일'을 대상 기간으로 자동 계산한다.
예) 9월에 실행 → 8월 1일~31일 기준 리포트.

사용법:
    py main.py

산출물:
    YYYY-MM_하이엔드주거_트렌드보고서.md / .xlsx  (현재 폴더, 4개 섹션을 합친 통합 리포트)
    data/YYYY-MM_monthly.json      (trend/amenity/facility/listings — (A) 월간 리포트와
                                     (B) 최신 인사이트 '26.1~현재 구간이 함께 참조하는 원본)
    data/YYYY-MM_global_case.json  (global_case — (B) 해외개발사례 아카이브 전용)
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from collect import collect
from dates import get_previous_month_period
from report import render_excel, render_markdown

OUTPUT_DIR = Path(__file__).parent
DATA_DIR = OUTPUT_DIR / "data"


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
    monthly_path = DATA_DIR / f"{period.label}_monthly.json"
    global_case_path = DATA_DIR / f"{period.label}_global_case.json"

    print(f"[3/3] 결과 저장 중: {md_path.name}, {xlsx_path.name}")
    md_path.write_text(render_markdown(sections, period), encoding="utf-8")
    render_excel(sections, period, xlsx_path)

    # (A) 월간 리포트 API와 (B) 최신 인사이트('26.1~현재)가 함께 참조하는 원본.
    # trend/amenity/facility/listings를 하나로 묶어 중복 저장을 피한다.
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    monthly_payload = {
        "label": period.label,
        "range_label": period.range_label,
        "year": period.year,
        "month": period.month,
        "generated_on": date.today().isoformat(),
        "trend": sections["trend"],
        "amenity": sections["amenity"],
        "facility": sections["facility"],
        "listings": sections["listings"],
    }
    monthly_path.write_text(
        json.dumps(monthly_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # global_case는 (A) 매물/순위와 성격이 달라 별도 파일로 유지한다.
    global_case_path.write_text(
        json.dumps(sections["global_case"], ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("완료.")
    print(f"  - {md_path}")
    print(f"  - {xlsx_path}")
    print(f"  - {monthly_path}")
    print(f"  - {global_case_path}")


if __name__ == "__main__":
    main()

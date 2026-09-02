"""리포트 대상 기간 계산.

월초(예: 9월 1일)에 실행하면 지난달 1일~말일(예: 8월 1일~8월 31일)을 대상 기간으로 계산한다.
언제 실행하든 항상 "오늘이 속한 달의 전월"을 기준으로 하므로, 스케줄러가 월초가 아닌
날짜에 실행되더라도 규칙은 동일하게 적용된다.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class ReportPeriod:
    year: int
    month: int
    start: date
    end: date

    @property
    def label(self) -> str:
        """파일명 등에 쓰는 'YYYY-MM' 형식."""
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def range_label(self) -> str:
        """리포트 본문에 쓰는 'YYYY.MM.01 ~ MM.DD' 형식."""
        return f"{self.start:%Y.%m.%d} ~ {self.end:%m.%d}"


def get_previous_month_period(today: date | None = None) -> ReportPeriod:
    """`today`가 속한 달의 바로 전월 1일~말일을 반환한다.

    `today`를 생략하면 실행 시점의 실제 날짜를 쓴다(운영 시 기본값).
    """
    if today is None:
        today = date.today()

    year, month = today.year, today.month
    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1

    last_day = calendar.monthrange(prev_year, prev_month)[1]
    start = date(prev_year, prev_month, 1)
    end = date(prev_year, prev_month, last_day)
    return ReportPeriod(year=prev_year, month=prev_month, start=start, end=end)

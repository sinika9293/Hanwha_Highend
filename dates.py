"""리포트 대상 기간 계산.

월초(예: 9월 1일)에 실행하면 지난달 1일~말일(예: 8월 1일~8월 31일)을 대상 기간으로 계산한다.
언제 실행하든 항상 "오늘이 속한 달의 전월"을 기준으로 하므로, 스케줄러가 월초가 아닌
날짜에 실행되더라도 규칙은 동일하게 적용된다.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta


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


def get_next_month_period(today: date | None = None) -> ReportPeriod:
    """`today`가 속한 달의 다음달 1일~말일을 반환한다. coming-soon 플레이스홀더 생성용."""
    if today is None:
        today = date.today()

    year, month = today.year, today.month
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1

    last_day = calendar.monthrange(next_year, next_month)[1]
    start = date(next_year, next_month, 1)
    end = date(next_year, next_month, last_day)
    return ReportPeriod(year=next_year, month=next_month, start=start, end=end)


@dataclass(frozen=True)
class WeekPeriod:
    year: int
    week_label: str  # 예: "2026-09-W2" — insight_index.json 포맷과 일치
    start: date  # 월요일
    end: date  # 일요일

    @property
    def range_label(self) -> str:
        """리포트 본문에 쓰는 'YYYY년 M월 D일 ~ M월 D일' 형식."""
        return (
            f"{self.start.year}년 {self.start.month}월 {self.start.day}일 ~ "
            f"{self.end.month}월 {self.end.day}일"
        )


def get_last_completed_week_period(today: date | None = None) -> WeekPeriod:
    """오늘 기준 가장 최근에 끝난 월요일 00:01~일요일 24:00 주간을 계산한다.

    `today`를 생략하면 실행 시점의 실제 날짜를 쓴다(운영 시 기본값). 주차 번호는
    "그 달의 몇 번째 월요일인가" 규칙으로 계산한다 — 기존 insight_index.json의
    실데이터(예: "2026-09-W2" = 9월 8일~14일, 9월의 2번째 월요일 시작 주)와 일치시키기
    위함이다.
    """
    if today is None:
        today = date.today()

    last_sunday = today - timedelta(days=today.isoweekday())  # isoweekday: 월=1..일=7
    last_monday = last_sunday - timedelta(days=6)
    week_no = (last_monday.day - 1) // 7 + 1
    week_label = f"{last_monday.year:04d}-{last_monday.month:02d}-W{week_no}"
    return WeekPeriod(
        year=last_monday.year, week_label=week_label, start=last_monday, end=last_sunday
    )

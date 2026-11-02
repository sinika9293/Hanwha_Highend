"""리포트 대상 기간 계산.

월초(예: 9월 1일)에 실행하면 지난달 1일~말일(예: 8월 1일~8월 31일)을 대상 기간으로 계산한다.
언제 실행하든 항상 "오늘이 속한 달의 전월"을 기준으로 하므로, 스케줄러가 월초가 아닌
날짜에 실행되더라도 규칙은 동일하게 적용된다.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

KST = timezone(timedelta(hours=9))


def today_kst() -> date:
    """한국 시간(Asia/Seoul, UTC+9) 기준 오늘 날짜. 실행 환경 시간대와 무관하다."""
    return datetime.now(KST).date()


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
        today = today_kst()

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
        today = today_kst()

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
    def end_exclusive(self) -> date:
        """다음 주 월요일. 주간 범위는 start 00:00 이상, end_exclusive 00:00 미만(KST)."""
        return self.end + timedelta(days=1)

    def contains(self, d: date) -> bool:
        return self.start <= d < self.end_exclusive

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
        today = today_kst()

    last_sunday = today - timedelta(days=today.isoweekday())  # isoweekday: 월=1..일=7
    last_monday = last_sunday - timedelta(days=6)
    week_no = (last_monday.day - 1) // 7 + 1
    week_label = f"{last_monday.year:04d}-{last_monday.month:02d}-W{week_no}"
    return WeekPeriod(
        year=last_monday.year, week_label=week_label, start=last_monday, end=last_sunday
    )


# ---------------------------------------------------------------------------
# 발행일 파싱 (KST 정규화 + 신뢰도)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedDate:
    date: date  # KST 기준 날짜
    confidence: str  # "high" | "medium" | "low"
    tz: str  # "explicit"(시간대 명시) | "assumed_kst"(KST 가정) | "none"(날짜만)
    method: str


# 주차 판정·날짜 근접 중복 비교에 쓸 수 있는 신뢰도. "low"(문장 속에서 찾아낸 날짜)는 쓰지 않는다.
USABLE_CONFIDENCE = ("high", "medium")

_DATE_PART = r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})\.?"
_TIME_PART = (
    r"(?:[T\s]+(\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?"
    r"\s*(Z|UTC|GMT|KST|[+-]\d{2}(?::?\d{2})?)?)?"
)
_PAREN = r"(?:\s*\([^)]*\))?"
_NUMERIC_RE = re.compile(rf"^{_DATE_PART}{_TIME_PART}{_PAREN}$", re.IGNORECASE)
_KOREAN_RE = re.compile(
    rf"^(\d{{4}})\s*년\s*(\d{{1,2}})\s*월\s*(\d{{1,2}})\s*일{_PAREN}"
    r"(?:\s*(?:오전|오후)?\s*\d{1,2}\s*시(?:\s*\d{1,2}\s*분)?)?$"
)
_KOREAN_TIME_RE = re.compile(r"\d\s*시")
_RFC822_RE = re.compile(r"^(?:[A-Za-z]{3},\s*)?\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{2}:\d{2}")
_AMBIGUOUS_RE = re.compile(r"^\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}\.?$")
_LOOSE_RE = re.compile(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})")


def _tz_from_token(token: str) -> timezone:
    t = token.upper()
    if t in ("Z", "UTC", "GMT"):
        return timezone.utc
    if t == "KST":
        return KST
    sign = 1 if t[0] == "+" else -1
    digits = t[1:].replace(":", "")
    hours = int(digits[:2])
    minutes = int(digits[2:4]) if len(digits) > 2 else 0
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def _from_numeric(m: re.Match) -> ParsedDate | None:
    base = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    hour = m.group(4)
    if hour is None:
        return ParsedDate(base, "high", "none", "date_only")
    h, mi, s = int(hour), int(m.group(5)), int(m.group(6) or 0)
    if mi > 59 or s > 59 or h > 24 or (h == 24 and (mi or s)):
        return None
    shift = timedelta(0)
    if h == 24:  # T24:00 은 다음 날 00:00
        h, shift = 0, timedelta(days=1)
    naive = datetime(base.year, base.month, base.day, h, mi, s) + shift
    token = m.group(7)
    if not token:
        return ParsedDate(naive.date(), "medium", "assumed_kst", "datetime_naive")
    local = naive.replace(tzinfo=_tz_from_token(token)).astimezone(KST)
    return ParsedDate(local.date(), "high", "explicit", "datetime_tz")


def parse_published(value) -> ParsedDate | None:
    """발행일 문자열을 KST 날짜와 신뢰도로 해석한다. 해석할 수 없으면 None.

    - high  : 문자열 전체가 날짜(요일 괄호 허용)이거나 시간대가 명시된 시각(KST로 변환), RFC 822
    - medium: 시간대 없는 시각(KST 가정), 한글 시각 표현이 붙은 날짜
    - low   : 긴 문장 속에서 날짜 모양 숫자만 찾아낸 경우 (주차 판정·근접 중복 비교에 쓰지 않는다)
    - None  : 모호한 형식(9/23/2026, 23.09.2026), 연-월만, 연도만, 8자리 숫자 단독 등
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    try:
        m = _NUMERIC_RE.match(s)
        if m:
            return _from_numeric(m)
        m = _KOREAN_RE.match(s)
        if m:
            base = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if _KOREAN_TIME_RE.search(s):
                return ParsedDate(base, "medium", "assumed_kst", "korean_datetime")
            return ParsedDate(base, "high", "none", "korean_date")
        if _RFC822_RE.match(s):
            dt = parsedate_to_datetime(s)
            if dt.tzinfo is None:
                return ParsedDate(dt.date(), "medium", "assumed_kst", "rfc822_naive")
            return ParsedDate(dt.astimezone(KST).date(), "high", "explicit", "rfc822")
        if _AMBIGUOUS_RE.match(s):
            return None
        m = _LOOSE_RE.search(s)
        if m:
            base = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return ParsedDate(base, "low", "none", "embedded")
    except (ValueError, OverflowError, TypeError):
        return None
    return None


def date_failure_reason(value) -> str:
    """parse_published 가 None 일 때의 제외 사유 코드."""
    if not value:
        return "date_missing"
    if isinstance(value, str) and _AMBIGUOUS_RE.match(value.strip()):
        return "date_ambiguous"
    return "date_unparseable"

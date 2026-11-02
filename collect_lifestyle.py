"""Claude API 웹검색으로 럭셔리 라이프스타일 10개 카테고리를 조사한다.

collect.py의 2단계(research_section → structure_section) 패턴을 그대로 재사용한다.
RESEARCH_SYSTEM_PROMPT/STRUCTURE_SYSTEM_PROMPT는 카테고리 중립적이라 수정 없이
import해서 쓴다. 다른 점은 조사 지시문(LIFESTYLE_RESEARCH_PROMPTS)과 제출 도구
(LIFESTYLE_SUBMIT_TOOLS)뿐이다.
"""

from __future__ import annotations

from backends import AIBackend, get_backend, mask_secrets
from dates import WeekPeriod
from lifestyle_schema import LIFESTYLE_KEYS, LIFESTYLE_RESEARCH_PROMPTS, LIFESTYLE_SUBMIT_TOOLS


def research_category(backend: AIBackend, period: WeekPeriod, category_key: str) -> str:
    """1단계: web_search로 해당 카테고리의 사실을 조사해 자유 텍스트 메모를 만든다."""
    prompt_template = LIFESTYLE_RESEARCH_PROMPTS[category_key]
    user_prompt = prompt_template.format(range_label=period.range_label, label=period.week_label)

    memo = backend.research(user_prompt).strip()

    if not memo:
        raise RuntimeError(
            f"[{category_key}] 1단계 조사에서 텍스트 응답을 받지 못했습니다. "
            "API 키/모델명/네트워크 상태를 확인하세요."
        )
    return memo


def structure_category(
    backend: AIBackend, period: WeekPeriod, category_key: str, memo: str
) -> dict:
    """2단계: 조사 메모를 해당 카테고리의 submit_* 스키마 JSON으로 강제 변환한다."""
    tool = LIFESTYLE_SUBMIT_TOOLS[category_key]
    return backend.structure(
        (
            f"대상 기간: {period.range_label} ({period.week_label})\n\n"
            f"조사 메모:\n{memo}"
        ),
        tool,
    )


def collect_category(backend: AIBackend, period: WeekPeriod, category_key: str) -> dict:
    """카테고리 하나에 대해 조사부터 구조화까지 실행한다."""
    memo = research_category(backend, period, category_key)
    return structure_category(backend, period, category_key, memo)


def collect_lifestyle(
    period: WeekPeriod,
    failures: dict[str, str] | None = None,
    backend: AIBackend | None = None,
) -> dict[str, dict]:
    """10개 라이프스타일 카테고리를 조사·구조화해 반환한다.

    카테고리별로 오류를 격리한다. 실패한 카테고리는 결과에서 빠지고, 사유가
    `failures`(전달 시)에 기록된다.
    """
    if backend is None:
        backend = get_backend()
    results: dict[str, dict] = {}
    for key in LIFESTYLE_KEYS:
        try:
            results[key] = collect_category(backend, period, key)
        except Exception as e:
            if failures is None:
                raise
            failures[key] = f"{type(e).__name__}: {mask_secrets(e)}"
    return results

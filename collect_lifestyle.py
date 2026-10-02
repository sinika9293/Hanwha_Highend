"""Claude API 웹검색으로 럭셔리 라이프스타일 10개 카테고리를 조사한다.

collect.py의 2단계(research_section → structure_section) 패턴을 그대로 재사용한다.
RESEARCH_SYSTEM_PROMPT/STRUCTURE_SYSTEM_PROMPT는 카테고리 중립적이라 수정 없이
import해서 쓴다. 다른 점은 조사 지시문(LIFESTYLE_RESEARCH_PROMPTS)과 제출 도구
(LIFESTYLE_SUBMIT_TOOLS)뿐이다.
"""

from __future__ import annotations

from anthropic import Anthropic

from collect import MAX_SEARCHES_PER_SECTION, MODEL_RESEARCH, MODEL_STRUCTURE
from collect import RESEARCH_SYSTEM_PROMPT, STRUCTURE_SYSTEM_PROMPT
from dates import WeekPeriod
from lifestyle_schema import LIFESTYLE_KEYS, LIFESTYLE_RESEARCH_PROMPTS, LIFESTYLE_SUBMIT_TOOLS


def research_category(client: Anthropic, period: WeekPeriod, category_key: str) -> str:
    """1단계: web_search로 해당 카테고리의 사실을 조사해 자유 텍스트 메모를 만든다."""
    prompt_template = LIFESTYLE_RESEARCH_PROMPTS[category_key]
    user_prompt = prompt_template.format(range_label=period.range_label, label=period.week_label)

    response = client.messages.create(
        model=MODEL_RESEARCH,
        max_tokens=8000,
        system=RESEARCH_SYSTEM_PROMPT,
        tools=[
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": MAX_SEARCHES_PER_SECTION,
                "user_location": {
                    "type": "approximate",
                    "country": "KR",
                },
            }
        ],
        messages=[{"role": "user", "content": user_prompt}],
    )

    memo_parts = [block.text for block in response.content if block.type == "text"]
    memo = "\n\n".join(memo_parts).strip()

    if not memo:
        raise RuntimeError(
            f"[{category_key}] 1단계 조사에서 텍스트 응답을 받지 못했습니다. "
            "API 키/모델명/네트워크 상태를 확인하세요."
        )
    return memo


def structure_category(
    client: Anthropic, period: WeekPeriod, category_key: str, memo: str
) -> dict:
    """2단계: 조사 메모를 해당 카테고리의 submit_* 스키마 JSON으로 강제 변환한다."""
    tool = LIFESTYLE_SUBMIT_TOOLS[category_key]
    response = client.messages.create(
        model=MODEL_STRUCTURE,
        max_tokens=8000,
        system=STRUCTURE_SYSTEM_PROMPT,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[
            {
                "role": "user",
                "content": (
                    f"대상 기간: {period.range_label} ({period.week_label})\n\n"
                    f"조사 메모:\n{memo}"
                ),
            }
        ],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == tool["name"]:
            return block.input

    raise RuntimeError(
        f"[{category_key}] 2단계 구조화에서 {tool['name']} 도구 호출을 받지 못했습니다. "
        "모델이 tool_choice 강제를 따르지 않았을 수 있습니다."
    )


def collect_category(client: Anthropic, period: WeekPeriod, category_key: str) -> dict:
    """카테고리 하나에 대해 조사부터 구조화까지 실행한다."""
    memo = research_category(client, period, category_key)
    return structure_category(client, period, category_key, memo)


def collect_lifestyle(period: WeekPeriod) -> dict[str, dict]:
    """10개 라이프스타일 카테고리 전체를 조사·구조화해 반환한다."""
    client = Anthropic()  # ANTHROPIC_API_KEY 환경변수를 자동으로 사용
    return {key: collect_category(client, period, key) for key in LIFESTYLE_KEYS}

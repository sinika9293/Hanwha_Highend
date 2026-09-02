"""Claude API 웹검색으로 하이엔드 주거 트렌드를 조사한다.

섹션(trend/amenity/facility/global_case)마다 독립적으로 2단계를 반복한다.
  1) research_section() — web_search 도구로 국내·해외 매체를 실제로 검색시켜
     그 섹션에 대한 인용 가능한 조사 메모(자유 텍스트)를 받는다.
  2) structure_section() — 조사 메모를 해당 섹션의 submit_* 스키마에 맞춰
     강제로 구조화된 JSON으로 변환시킨다.

두 단계로 나눈 이유: 검색 도구(web_search)와 "무조건 이 도구를 호출하라"는
tool_choice 강제를 같은 요청에 섞으면 검색을 충분히 못 하고 조기에 종료되기 쉽다.
검색은 자유롭게 시키고, 구조화는 별도 요청에서 강제한다.
섹션을 나눈 이유: 섹션마다 검색 주제가 뚜렷이 달라 한 번에 몰아서 조사시키면
특정 섹션(특히 후순위 항목)이 부실해지기 쉽다. 섹션별로 별도 조사를 시키면
각 섹션이 균등한 검색 예산을 받는다.
"""

from __future__ import annotations

import os

from anthropic import Anthropic

from dates import ReportPeriod
from schema import SECTION_KEYS, SUBMIT_TOOLS

# 웹검색을 많이 시켜야 하는 1단계는 비용 대비 성능이 좋은 모델을,
# 이미 모아진 텍스트를 JSON으로 옮기기만 하는 2단계는 더 가벼운 모델을 써도 된다.
# 필요하면 .env 에 아래 두 변수를 넣어 덮어쓸 수 있다.
MODEL_RESEARCH = os.environ.get("CLAUDE_MODEL_RESEARCH", "claude-sonnet-5")
MODEL_STRUCTURE = os.environ.get("CLAUDE_MODEL_STRUCTURE", "claude-sonnet-5")

# 섹션 하나당 조사 범위가 좁아졌으므로 섹션별 검색 상한은 기존보다 줄인다.
MAX_SEARCHES_PER_SECTION = int(os.environ.get("CLAUDE_MAX_SEARCHES", "12"))

RESEARCH_SYSTEM_PROMPT = """\
당신은 국내외 하이엔드(초고가) 주거·부동산 시장을 취재하는 리서처입니다.
web_search 도구를 적극적으로 사용해 아래 항목을 조사하고, 매체명과 보도일을
반드시 함께 표기한 상세 메모를 작성하세요. 추측하지 말고, 검색으로 확인한
사실만 쓰세요. 확인하지 못한 항목은 "확인되지 않음"이라고 명시하세요.
"""

STRUCTURE_SYSTEM_PROMPT = """\
당신은 리서치 메모를 고정된 리포트 템플릿 구조로 정리하는 편집자입니다.
주어진 조사 메모의 사실만 사용하고 새로운 사실을 지어내지 마세요.
메모에 없는 항목은 빈 배열이나 "확인되지 않음"으로 채우세요.
반드시 지정된 도구를 한 번 호출해 결과를 제출하세요.
"""

SECTION_RESEARCH_PROMPTS = {
    "trend": """\
조사 대상 기간: {range_label} ({label})

'월간 하이엔드 주거 동향'을 이 기간을 중심으로 조사하세요. 필요하면 직전
비교 시점(전월, 상반기 등) 데이터도 함께 찾아 비교하세요.

1. 국내 하이엔드 청약 경쟁률
   - 하이엔드 단지 vs 일반 아파트, 브랜드 vs 비브랜드 1순위 평균 경쟁률
   - 이 기간 개별 단지 중 최고/최저 경쟁률 사례 (단지명, 수치, 배경)
   - 이 기간 청약 결산: 공급 물량, 마감/미달 단지, 분양가와 시세 갭 등 핵심 변수

2. 정책·세제 변화(양도세, 대출 규제, 임대형 하이엔드 등)와 시장 영향

3. 리스크 신호: 관리비 상승, 서비스 재계약/유료화 분쟁, 브랜드 사용권 갈등 등

4. 위 내용을 바탕으로 이번 기간에 특히 관심도가 높아진 키워드 5~9개와,
   각 키워드가 왜 지금 부각되는지, 이를 뒷받침하는 지표/사례를 정리하세요.

5. 다음 달 이후 확인해봐야 할 관전 포인트를 정리하세요.

6. 해외 하이엔드 주거시장에서 이번 기간 주목할 핵심 동향/키워드 3~5개와
   그 근거(보도, 사례, 지표)를 정리하세요.

마지막에는 인용한 모든 출처를 "매체명(날짜) — 무엇을 보도했는지" 형식으로
목록화하세요.
""",
    "amenity": """\
조사 대상 기간: {range_label} ({label})

'최신 어메니티 트렌드(서비스·운영)'을 이 기간을 중심으로 조사하세요.
마감재 경쟁이 아니라 운영·서비스 경쟁으로 이동하는 사례에 집중하세요.

- 컨시어지 상시 운영, 개인 소믈리에·버틀러 서비스
- 커뮤니티 프로그램(문화·교육·피트니스 클래스 등)
- 멤버십·회원제 라운지, 조식·다이닝 서비스
- 시니어 레지던스의 웰니스·메디컬 결합 서비스
- 초고가 신상품, 리테일·럭셔리 브랜드의 주거 진출과 결합된 서비스 사례

국내 사례와 해외(해외 하이엔드·브랜디드 레지던스) 사례를 각각 최소 2건
이상 포함해 조사하고, 각 사례가 국내/해외 중 어디인지 명확히 구분해
메모하세요. 총 최소 4개 이상의 구체적 단지/서비스 사례(단지명, 국내/해외
구분, 서비스 내용, 시점)를 찾아 정리하세요. 마지막에는 인용한 모든 출처를
"매체명(날짜) — 무엇을 보도했는지" 형식으로 목록화하세요.
""",
    "facility": """\
조사 대상 기간: {range_label} ({label})

'떠오르는 부대시설(하드웨어 공간)'을 이 기간을 중심으로 조사하세요.
서비스가 아니라 새로 설치되거나 화제가 된 물리적 공간·시설에 집중하세요.

- 웰니스 센터, 스파, 실내 수영장, 사우나
- 스카이라운지, 루프탑, 프라이빗 다이닝룸
- 펫파크, 펫 전용 시설
- 게스트하우스, 코워킹 스페이스, 시네마룸 등 신설 커뮤니티 시설
- EV 충전 등 신규 인프라 시설

국내 사례와 해외(해외 하이엔드·브랜디드 레지던스) 사례를 각각 최소 2건
이상 포함해 조사하고, 각 사례가 국내/해외 중 어디인지 명확히 구분해
메모하세요. 총 최소 4개 이상의 구체적 단지/시설 사례(단지명, 국내/해외
구분, 시설 스펙, 시점)를 찾아 정리하세요. 마지막에는 인용한 모든 출처를
"매체명(날짜) — 무엇을 보도했는지" 형식으로 목록화하세요.
""",
    "global_case": """\
조사 대상 기간: {range_label} ({label})

'해외 개발 사례'를 이 기간을 중심으로 조사하세요.

1. 해외 하이엔드·브랜디드 레지던스 시장 규모, 주요 리서치하우스 보고서 논지
2. 이 기간 발표된 주요 딜/거래 (프로젝트명, 지역, 금액, 내용) — 최소 3건 이상
3. 건축·설계 트렌드 (Dezeen, Architectural Digest 등)

마지막에는 인용한 모든 출처를 "매체명(날짜) — 무엇을 보도했는지" 형식으로
목록화하세요.
""",
}


def research_section(client: Anthropic, period: ReportPeriod, section_key: str) -> str:
    """1단계: web_search로 해당 섹션의 사실을 조사해 자유 텍스트 메모를 만든다."""
    prompt_template = SECTION_RESEARCH_PROMPTS[section_key]
    user_prompt = prompt_template.format(range_label=period.range_label, label=period.label)

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
            f"[{section_key}] 1단계 조사에서 텍스트 응답을 받지 못했습니다. "
            "API 키/모델명/네트워크 상태를 확인하세요."
        )
    return memo


def structure_section(client: Anthropic, period: ReportPeriod, section_key: str, memo: str) -> dict:
    """2단계: 조사 메모를 해당 섹션의 submit_* 스키마 JSON으로 강제 변환한다."""
    tool = SUBMIT_TOOLS[section_key]
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
                    f"대상 기간: {period.range_label} ({period.label})\n\n"
                    f"조사 메모:\n{memo}"
                ),
            }
        ],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == tool["name"]:
            return block.input

    raise RuntimeError(
        f"[{section_key}] 2단계 구조화에서 {tool['name']} 도구 호출을 받지 못했습니다. "
        "모델이 tool_choice 강제를 따르지 않았을 수 있습니다."
    )


def collect_section(client: Anthropic, period: ReportPeriod, section_key: str) -> dict:
    """섹션 하나에 대해 조사부터 구조화까지 실행한다."""
    memo = research_section(client, period, section_key)
    return structure_section(client, period, section_key, memo)


def collect(period: ReportPeriod) -> dict[str, dict]:
    """4개 섹션 전체를 조사·구조화해 반환한다. main.py 에서 호출."""
    client = Anthropic()  # ANTHROPIC_API_KEY 환경변수를 자동으로 사용
    return {key: collect_section(client, period, key) for key in SECTION_KEYS}

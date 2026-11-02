"""최신 인사이트 4개 섹션(trend/amenity/facility/global_case)의 주간 조사.

collect.py(월간)·collect_lifestyle.py와 같은 2단계(web_search 조사 → 강제 tool_choice 구조화)
패턴을 재사용한다. 제출 스키마는 사이트가 읽는 기존 주간 JSON 형태(domestic/global)를 따르며,
검증용 `date`(기사 발행일)를 항목마다 받는다. `date`는 저장 전에 SAVE_FIELDS 기준으로 걸러진다
(기존 주간 JSON에 date가 없던 섹션에는 저장하지 않는다).
"""

from __future__ import annotations

from backends import AIBackend, get_backend, mask_secrets
from dates import WeekPeriod
from schema import SECTION_KEYS

FACILITY_GROUPS = ("popular", "emerging")

TITLE_KEY = {"trend": "title", "amenity": "title", "facility": "name", "global_case": "project"}

# 저장 시 남기는 필드 = 기존 data/{week}_{section}.json 항목의 필드 (image_url 제외)
SAVE_FIELDS = {
    "trend": ("title", "summary", "source", "source_url", "date"),
    "amenity": ("title", "summary", "facility_type", "source", "source_url"),
    "facility": ("name", "trend", "summary", "source_url"),
    "global_case": ("project", "location", "developer", "summary", "highlight", "source_url"),
}


def _s(desc: str = "") -> dict:
    return {"type": "string", "description": desc} if desc else {"type": "string"}


_URL = _s("이 항목의 근거가 된 원문 기사 URL. 확인 못 하면 빈 문자열.")
_DATE = _s("근거 기사의 발행일 YYYY-MM-DD. 확인 못 하면 빈 문자열.")

_ITEM_PROPS = {
    "trend": {
        "title": _s("1~2줄로 요약 가능한 제목"),
        "summary": _s(),
        "source": _s("매체명"),
        "source_url": _URL,
        "date": _DATE,
    },
    "amenity": {
        "title": _s("단지명·서비스명을 포함한 제목"),
        "summary": _s(),
        "facility_type": _s("시설/서비스 유형 (예: 라운지, 스포츠)"),
        "source": _s("매체명"),
        "source_url": _URL,
        "date": _DATE,
    },
    "facility": {
        "name": _s("부대시설 트렌드명"),
        "trend": _s("추세 (예: 지속 강세, 확산, 신규 등장)"),
        "summary": _s(),
        "source_url": _URL,
        "date": _DATE,
    },
    "global_case": {
        "project": _s("프로젝트·단지명"),
        "location": _s("소재지"),
        "developer": _s("시행·시공사"),
        "summary": _s(),
        "highlight": _s("한 줄 핵심 포인트"),
        "source_url": _URL,
        "date": _DATE,
    },
}

_REQUIRED = {
    "trend": ["title", "summary", "source", "date"],
    "amenity": ["title", "summary", "facility_type", "source", "date"],
    "facility": ["name", "trend", "summary", "date"],
    "global_case": ["project", "summary", "date"],
}


def _array(section: str) -> dict:
    # minItems를 두지 않는다: 기간 내 기사가 적으면 적은 대로 제출해야 하고, 오래된 기사로 채우면 안 된다.
    return {
        "type": "array",
        "items": {"type": "object", "required": _REQUIRED[section], "properties": _ITEM_PROPS[section]},
    }


def _region_schema(section: str) -> dict:
    if section == "facility":
        return {
            "type": "object",
            "required": list(FACILITY_GROUPS),
            "properties": {g: _array(section) for g in FACILITY_GROUPS},
        }
    return _array(section)


def _section_schema(section: str) -> dict:
    return {
        "type": "object",
        "required": ["range_label", "domestic", "global"],
        "properties": {
            "range_label": _s(),
            "domestic": _region_schema(section),
            "global": _region_schema(section),
        },
    }


INSIGHT_SUBMIT_TOOLS = {
    key: {
        "name": f"submit_weekly_{key}",
        "description": f"'{key}' 섹션의 이번 주 조사 결과를 제출한다.",
        "input_schema": _section_schema(key),
    }
    for key in SECTION_KEYS
}

_RULES = """\

[필수 규칙]
- 발행일이 {range_label} 안에 있는 기사만 사용하세요. 기간 밖 기사로 항목을 채우지 마세요.
- 기간 내 기사가 부족하면 부족한 대로 적게 쓰세요(0건도 허용). 오래된 기사를 끼워 넣지 마세요.
- 항목마다 근거 기사의 매체명, 발행일(YYYY-MM-DD), 원문 URL을 괄호로 표기하세요.
- 국내 사례와 해외 사례를 구분해 각각 정리하세요.
"""

INSIGHT_RESEARCH_PROMPTS = {
    "trend": """\
조사 대상 기간: {range_label} ({label})

'하이엔드 주거 트렌드' 관련 국내·해외 기사를 조사하세요. 청약·시세 신고가, 브랜드 경쟁,
정책·세제, 리스크 신호(관리비, 시공사 교체, 브랜드 갈등)와 해외 하이엔드 주거시장 동향을 다룹니다.
""" + _RULES,
    "amenity": """\
조사 대상 기간: {range_label} ({label})

하이엔드 주거단지의 어메니티(컨시어지, 커뮤니티 프로그램, 멤버십, 다이닝 등 서비스·운영)
사례를 국내·해외로 나눠 조사하세요.
""" + _RULES,
    "facility": """\
조사 대상 기간: {range_label} ({label})

하이엔드 주거단지의 부대시설(하드웨어 공간: 웰니스센터, 스카이라운지, 프라이빗 다이닝룸,
펫 시설 등) 트렌드를 국내·해외로 나눠 조사하세요. 각 트렌드를 popular(이미 인기·지속 강세)와
emerging(새로 등장)으로 분류하세요. 같은 기사에서 여러 트렌드가 나올 수 있습니다.
""" + _RULES,
    "global_case": """\
조사 대상 기간: {range_label} ({label})

하이엔드 주거의 개발·운영 사례를 조사하세요. 국내는 해외형 개발·운영 모델을 도입한 단지,
해외는 브랜디드 레지던스·하이엔드 프로젝트입니다. 프로젝트명, 소재지, 시행·시공사를 확인하세요.
""" + _RULES,
}


def research_insight(backend: AIBackend, period: WeekPeriod, section: str) -> str:
    user_prompt = INSIGHT_RESEARCH_PROMPTS[section].format(
        range_label=period.range_label, label=period.week_label
    )
    memo = backend.research(user_prompt).strip()
    if not memo:
        raise RuntimeError(f"[{section}] 1단계 조사에서 텍스트 응답을 받지 못했습니다.")
    return memo


def structure_insight(backend: AIBackend, period: WeekPeriod, section: str, memo: str) -> dict:
    tool = INSIGHT_SUBMIT_TOOLS[section]
    return backend.structure(
        (
            f"대상 기간: {period.range_label} ({period.week_label})\n"
            "각 항목의 date에는 메모에 적힌 기사 발행일을 YYYY-MM-DD로 옮기고, "
            "메모에 발행일이 없으면 빈 문자열로 두세요. 기간 밖 기사는 포함하지 마세요.\n\n"
            f"조사 메모:\n{memo}"
        ),
        tool,
    )


def collect_insight_section(backend: AIBackend, period: WeekPeriod, section: str) -> dict:
    return structure_insight(backend, period, section, research_insight(backend, period, section))


def collect_insight(
    period: WeekPeriod,
    failures: dict[str, str] | None = None,
    sections=SECTION_KEYS,
    backend: AIBackend | None = None,
) -> dict[str, dict]:
    """섹션별로 오류를 격리해 조사한다. 실패 섹션은 결과에서 빠지고 사유가 failures에 기록된다."""
    if backend is None:
        backend = get_backend()
    results: dict[str, dict] = {}
    for key in sections:
        try:
            results[key] = collect_insight_section(backend, period, key)
        except Exception as e:
            if failures is None:
                raise
            failures[key] = f"{type(e).__name__}: {mask_secrets(e)}"
    return results

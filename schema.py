"""4개 섹션으로 분리된 리포트 템플릿의 구조.

기존에는 청약·상품트렌드·해외동향을 하나의 스키마로 묶었지만,
이제 섹션마다 독립된 웹검색 → 구조화가 이뤄지도록 4개로 나눈다.
  - trend        : 월간 하이엔드 주거 동향 (청약·정책·키워드 등)
  - amenity      : 최신 어메니티 트렌드 (서비스·운영)
  - facility     : 떠오르는 부대시설 (하드웨어 공간)
  - global_case  : 해외 개발 사례

각 스키마는 Claude에게 해당 섹션의 submit_* 도구로 강제 호출시켜 받아내는
최종 데이터의 형태이며, report.py 가 이 4개를 그대로 markdown/Excel로 렌더링한다.
"""

TREND_SCHEMA = {
    "type": "object",
    "required": [
        "executive_summary",
        "subscription_table",
        "subscription_note",
        "monthly_summary",
        "policy_tax",
        "risk_signals",
        "keywords",
        "watchpoints",
        "sources",
    ],
    "properties": {
        "executive_summary": {
            "type": "array",
            "description": "한눈에 보기 — 핵심 요약 3~6개, 각 항목은 굵게 표시할 핵심어를 포함한 완결된 문장.",
            "items": {"type": "string"},
            "minItems": 3,
            "maxItems": 6,
        },
        "subscription_table": {
            "type": "array",
            "description": "청약 경쟁률 비교 표 (하이엔드/일반, 브랜드/비브랜드 등 해당 월에 확인 가능한 구분).",
            "items": {
                "type": "object",
                "required": ["label", "listings", "applicants", "competition_rate"],
                "properties": {
                    "label": {"type": "string", "description": "구분 (예: 하이엔드, 일반 아파트, 브랜드, 비브랜드)"},
                    "listings": {"type": "string", "description": "모집 세대수"},
                    "applicants": {"type": "string", "description": "접수 인원"},
                    "competition_rate": {"type": "string", "description": "1순위 평균 경쟁률 (예: '20.31 : 1')"},
                },
            },
            "minItems": 1,
        },
        "subscription_note": {
            "type": "string",
            "description": "개별 단지 경쟁률(최고·최저 사례)과 해석을 담은 서술 단락.",
        },
        "monthly_summary": {
            "type": "string",
            "description": "해당 월 청약 결산 — 공급 물량, 마감/미달 단지, 핵심 변수(분양가 등) 서술.",
        },
        "policy_tax": {
            "type": "string",
            "description": "정책·세제 변수와 하이엔드 시장에 대한 영향.",
        },
        "risk_signals": {
            "type": "array",
            "description": "리스크 신호 (관리비, 서비스 지속가능성, 브랜드 갈등 등).",
            "items": {"type": "string"},
        },
        "keywords": {
            "type": "array",
            "description": "관심도가 높은 키워드 표 (순위 내림차순 없이 중요도순).",
            "items": {
                "type": "object",
                "required": ["rank", "keyword", "why_now", "indicator"],
                "properties": {
                    "rank": {"type": "integer"},
                    "keyword": {"type": "string"},
                    "why_now": {"type": "string", "description": "왜 지금 부각되는지"},
                    "indicator": {"type": "string", "description": "확인 가능한 지표/사례"},
                },
            },
            "minItems": 5,
        },
        "watchpoints": {
            "type": "array",
            "description": "다음 달 이후 확인해야 할 관전 포인트.",
            "items": {"type": "string"},
            "minItems": 3,
        },
        "sources": {
            "type": "array",
            "description": "국내 출처. '매체명(날짜) — 내용' 형식.",
            "items": {"type": "string"},
        },
    },
}

AMENITY_SCHEMA = {
    "type": "object",
    "required": ["intro", "items", "sources"],
    "properties": {
        "intro": {
            "type": "string",
            "description": "어메니티(서비스·운영) 트렌드 개요 — 마감재에서 운영으로 이동하는 경쟁축을 서술.",
        },
        "items": {
            "type": "array",
            "description": "컨시어지·커뮤니티 프로그램·멤버십·다이닝 등 서비스·운영 트렌드 사례.",
            "items": {
                "type": "object",
                "required": ["name", "description"],
                "properties": {
                    "name": {"type": "string", "description": "단지명 또는 서비스명"},
                    "description": {"type": "string", "description": "핵심 내용과 특징"},
                },
            },
            "minItems": 4,
        },
        "sources": {
            "type": "array",
            "description": "출처. '매체명(날짜) — 내용' 형식.",
            "items": {"type": "string"},
        },
    },
}

FACILITY_SCHEMA = {
    "type": "object",
    "required": ["intro", "items", "sources"],
    "properties": {
        "intro": {
            "type": "string",
            "description": "부대시설(하드웨어 공간) 트렌드 개요 — 새로 등장하는 물리적 공간·시설의 흐름을 서술.",
        },
        "items": {
            "type": "array",
            "description": "웰니스센터·스카이라운지·프라이빗 다이닝룸·펫파크 등 신설·부상 중인 부대시설 사례.",
            "items": {
                "type": "object",
                "required": ["name", "description"],
                "properties": {
                    "name": {"type": "string", "description": "단지명 또는 시설명"},
                    "description": {"type": "string", "description": "핵심 스펙과 특징"},
                },
            },
            "minItems": 4,
        },
        "sources": {
            "type": "array",
            "description": "출처. '매체명(날짜) — 내용' 형식.",
            "items": {"type": "string"},
        },
    },
}

GLOBAL_CASE_SCHEMA = {
    "type": "object",
    "required": ["market_overview", "deals", "design_trends", "sources"],
    "properties": {
        "market_overview": {
            "type": "string",
            "description": "해외 브랜디드 레지던스/하이엔드 시장 개요와 논지.",
        },
        "deals": {
            "type": "array",
            "description": "해당 월 주요 해외 딜 목록.",
            "items": {
                "type": "object",
                "required": ["project", "region", "news"],
                "properties": {
                    "project": {"type": "string"},
                    "region": {"type": "string"},
                    "news": {"type": "string"},
                },
            },
            "minItems": 1,
        },
        "design_trends": {
            "type": "string",
            "description": "건축·설계 트렌드 (Dezeen/AD 등) 서술 단락.",
        },
        "sources": {
            "type": "array",
            "description": "해외 출처. '매체명(날짜) — 내용' 형식.",
            "items": {"type": "string"},
        },
    },
}

SUBMIT_TOOLS = {
    "trend": {
        "name": "submit_trend",
        "description": "조사한 월간 하이엔드 주거 동향을 고정 템플릿 구조로 최종 제출한다.",
        "input_schema": TREND_SCHEMA,
    },
    "amenity": {
        "name": "submit_amenity",
        "description": "조사한 어메니티(서비스·운영) 트렌드를 고정 템플릿 구조로 최종 제출한다.",
        "input_schema": AMENITY_SCHEMA,
    },
    "facility": {
        "name": "submit_facility",
        "description": "조사한 부대시설(하드웨어 공간) 트렌드를 고정 템플릿 구조로 최종 제출한다.",
        "input_schema": FACILITY_SCHEMA,
    },
    "global_case": {
        "name": "submit_global_case",
        "description": "조사한 해외 개발 사례를 고정 템플릿 구조로 최종 제출한다.",
        "input_schema": GLOBAL_CASE_SCHEMA,
    },
}

SECTION_KEYS = ("trend", "amenity", "facility", "global_case")

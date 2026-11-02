"""AI 백엔드 공통 인터페이스·예외·보안 유틸.

주간 수집 모듈(collect_insight / collect_lifestyle)은 공급자 SDK를 직접 알지 못하고
이 Protocol만 사용한다. 공급자별 SDK 임포트와 환경변수(API 키·모델) 처리는 각 어댑터 안에 격리한다.
"""

from __future__ import annotations

import os
import re
from typing import Protocol, runtime_checkable

ENV_BACKEND = "AI_BACKEND"
DEFAULT_BACKEND = "none"

# 예외 메시지·로그에서 값을 지워야 하는 환경변수 이름
SECRET_ENV_NAMES = ("ANTHROPIC_API_KEY",)


class BackendError(RuntimeError):
    """AI 백엔드 관련 오류의 공통 부모."""


class BackendDisabled(BackendError):
    """AI 백엔드가 비활성(none) 상태에서 호출됨."""


class BackendUnavailable(BackendError):
    """선택한 백엔드를 쓸 수 없음 (SDK 미설치, API 키 없음 등)."""


class BackendCallError(BackendError):
    """백엔드 호출 중 공급자 SDK 오류 (메시지는 마스킹됨)."""


class UnknownBackend(ValueError):
    """등록되지 않은 백엔드 이름."""


@runtime_checkable
class AIBackend(Protocol):
    name: str

    def available(self) -> tuple[bool, str]:
        """네트워크 호출 없이 SDK·키 유무만 점검한다. (사용 가능 여부, 사유)"""

    def research(self, user_prompt: str, *, max_searches: int | None = None) -> str:
        """조사 단계: 웹 검색 등으로 사실을 모아 자유 텍스트 메모를 반환한다."""

    def structure(self, user_prompt: str, tool: dict) -> dict:
        """구조화 단계: 메모를 tool 스키마에 맞는 dict로 변환해 반환한다."""


def resolve_backend_name(environ=None) -> str:
    """AI_BACKEND 환경변수에서 백엔드 이름을 얻는다. 미설정이면 'none'."""
    env = os.environ if environ is None else environ
    value = (env.get(ENV_BACKEND) or "").strip().lower()
    return value or DEFAULT_BACKEND


def load_env() -> bool:
    """python-dotenv가 설치돼 있으면 .env를 로드한다 (선택 의존성).

    미설치면 .env 자동 로딩은 일어나지 않으며, 환경변수는 실행 환경이 직접 제공해야 한다.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    return bool(load_dotenv())


_MASK = "[MASKED]"
_TOKEN_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*"),
]
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}")
# 헤더 줄 전체를 지우는 항목 (값에 공백·세미콜론이 섞여도 남기지 않는다)
_HEADER_LINE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(set-cookie|cookie|proxy-authorization|authorization)(['\"]?\s*[:=]\s*)[^\r\n]+"
)
# NAME=값 / NAME: 값 / "name": "값" / ?name=값  (이름 앞에 접두어가 붙어도 매칭: ANTHROPIC_API_KEY, GITHUB_TOKEN ...)
_KEY_VALUE = re.compile(
    r"(?i)([A-Za-z0-9_\-]*(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password|passwd|"
    r"credential|session(?:[_-]?id)?)s?)"
    r"(['\"]?\s*[:=]\s*)(['\"]?)(?![0-9]+(?:[\s'\",;)]|$))[^\s'\",;&]{4,}"
)
_WIN_USER_PATH = re.compile(r"[A-Za-z]:\\Users\\[^\\\s'\"]+")
_POSIX_USER_PATH = re.compile(r"/(?:home|Users)/[^/\s'\"]+")


def mask_secrets(text, environ=None) -> str:
    """로그·상태 파일에 남길 문자열에서 키·토큰·비밀번호·로컬 사용자 경로를 지운다."""
    s = str(text)
    env = os.environ if environ is None else environ
    for name in SECRET_ENV_NAMES:
        value = env.get(name)
        if value and len(value) >= 4:
            s = s.replace(value, _MASK)
    for pat in _TOKEN_PATTERNS:
        s = pat.sub(_MASK, s)
    s = _HEADER_LINE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_MASK}", s)
    s = _BEARER.sub(lambda m: f"{m.group(1)} {_MASK}", s)
    s = _KEY_VALUE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{_MASK}", s)
    s = _WIN_USER_PATH.sub("<local-path>", s)
    s = _POSIX_USER_PATH.sub("<local-path>", s)
    return s

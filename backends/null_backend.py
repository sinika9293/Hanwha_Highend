"""비활성 백엔드 — AI_BACKEND 미설정(none)일 때의 기본값. 어떤 외부 호출도 하지 않는다."""

from __future__ import annotations

from .base import BackendDisabled

_REASON = "AI backend is disabled (AI_BACKEND is unset or 'none')"


class NullBackend:
    name = "none"

    def available(self) -> tuple[bool, str]:
        return False, _REASON

    def research(self, user_prompt: str, *, max_searches: int | None = None) -> str:
        raise BackendDisabled(_REASON)

    def structure(self, user_prompt: str, tool: dict) -> dict:
        raise BackendDisabled(_REASON)

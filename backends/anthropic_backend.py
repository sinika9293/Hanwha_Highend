"""Anthropic 어댑터 — SDK 임포트·API 키·모델 환경변수 처리를 이 파일 안에 격리한다.

- 이 모듈을 임포트해도 anthropic SDK는 임포트되지 않는다 (호출 시점에 지연 임포트).
- 프롬프트·모델 기본값은 보호 파일 collect.py의 상수를 그대로 쓰되, 어댑터 안에서만 지연 로드한다
  (collect.py는 최상단에서 anthropic을 임포트하므로 SDK가 있을 때만 로드된다).
- 실제 API 호출은 AI_BACKEND=anthropic 이고 SDK·키가 모두 있을 때만 일어난다.
"""

from __future__ import annotations

import importlib.util
import os

from .base import BackendCallError, BackendError, BackendUnavailable, mask_secrets

KEY_ENV = "ANTHROPIC_API_KEY"


def _load_collect_constants():
    import collect  # 지연 임포트: SDK가 있어야만 성공한다

    return collect


class AnthropicBackend:
    name = "anthropic"

    def __init__(self, client_factory=None, environ=None, constants=None):
        self._client_factory = client_factory
        self._environ = os.environ if environ is None else environ
        self._constants = constants
        self._client = None

    def available(self) -> tuple[bool, str]:
        if self._client_factory is None and importlib.util.find_spec("anthropic") is None:
            return False, "anthropic SDK is not installed (pip install -r requirements.txt)"
        if not self._environ.get(KEY_ENV):
            return False, f"{KEY_ENV} is not set"
        return True, "ok"

    def _consts(self):
        if self._constants is None:
            try:
                self._constants = _load_collect_constants()
            except ImportError as e:
                raise BackendUnavailable(f"anthropic SDK is not installed ({e.__class__.__name__})") from None
        return self._constants

    def _get_client(self):
        if self._client is None:
            ok, reason = self.available()
            if not ok:
                raise BackendUnavailable(reason)
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                try:
                    from anthropic import Anthropic
                except ImportError:
                    raise BackendUnavailable("anthropic SDK is not installed") from None
                self._client = Anthropic()  # ANTHROPIC_API_KEY 환경변수를 SDK가 직접 사용
        return self._client

    def _call(self, **kwargs):
        try:
            return self._get_client().messages.create(**kwargs)
        except BackendError:
            raise
        except Exception as e:
            raise BackendCallError(f"{type(e).__name__}: {mask_secrets(e, self._environ)}") from None

    def research(self, user_prompt: str, *, max_searches: int | None = None) -> str:
        c = self._consts()
        response = self._call(
            model=c.MODEL_RESEARCH,
            max_tokens=8000,
            system=c.RESEARCH_SYSTEM_PROMPT,
            tools=[
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": max_searches if max_searches is not None else c.MAX_SEARCHES_PER_SECTION,
                    "user_location": {"type": "approximate", "country": "KR"},
                }
            ],
            messages=[{"role": "user", "content": user_prompt}],
        )
        return "\n\n".join(b.text for b in response.content if b.type == "text").strip()

    def structure(self, user_prompt: str, tool: dict) -> dict:
        c = self._consts()
        response = self._call(
            model=c.MODEL_STRUCTURE,
            max_tokens=8000,
            system=c.STRUCTURE_SYSTEM_PROMPT,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": user_prompt}],
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == tool["name"]:
                return block.input
        raise BackendCallError(f"structure step returned no {tool['name']} tool call")

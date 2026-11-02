"""AI 백엔드 레지스트리와 실행 진입 헬퍼.

기본값은 비활성(none)이다. 등록된 백엔드는 none, anthropic 둘뿐이며
신규 공급자는 사용자 별도 승인 없이 추가하지 않는다.
"""

from __future__ import annotations

from .anthropic_backend import AnthropicBackend
from .base import (
    AIBackend,
    BackendCallError,
    BackendDisabled,
    BackendError,
    BackendUnavailable,
    UnknownBackend,
    load_env,
    mask_secrets,
    resolve_backend_name,
)
from .null_backend import NullBackend

BACKENDS = {"none": NullBackend, "anthropic": AnthropicBackend}


def get_backend(name: str | None = None) -> AIBackend:
    name = resolve_backend_name() if name is None else name.strip().lower()
    factory = BACKENDS.get(name)
    if factory is None:
        raise UnknownBackend(f"unknown AI_BACKEND {name!r} (allowed: {', '.join(sorted(BACKENDS))})")
    return factory()


def _report(log, status: str, changed: str, code: int, reason: str) -> None:
    log(f"STATUS={status}")
    log(f"DATA_CHANGED={changed}")
    log(f"EXIT_CODE={code}")
    log(f"REASON={mask_secrets(reason)}")


def prepare_backend(log=print):
    """실행 시작 시 백엔드를 결정한다. 반환: (backend | None, exit_code | None).

    - none        -> (None, 0)  STATUS=SKIPPED, 데이터 무변경
    - 오류/사용불가 -> (None, 1)  STATUS=ERROR, 데이터 무변경
    - 사용 가능    -> (backend, None)
    """
    load_env()
    name = resolve_backend_name()
    if name == "none":
        _report(log, "SKIPPED", "false", 0, "AI_BACKEND is unset or 'none'; nothing was collected or modified")
        return None, 0
    try:
        backend = get_backend(name)
    except UnknownBackend as e:
        _report(log, "ERROR", "false", 1, str(e))
        return None, 1
    ok, reason = backend.available()
    if not ok:
        _report(log, "ERROR", "false", 1, f"AI_BACKEND={name}: {reason}")
        return None, 1
    return backend, None


__all__ = [
    "AIBackend", "AnthropicBackend", "BACKENDS", "BackendCallError", "BackendDisabled", "BackendError",
    "BackendUnavailable", "NullBackend", "UnknownBackend", "get_backend", "load_env", "mask_secrets",
    "prepare_backend", "resolve_backend_name",
]

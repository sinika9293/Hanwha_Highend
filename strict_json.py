"""엄격한 JSON 로더 — 향후 P5 정책 JSON(소스 레지스트리, AI 정책 등) 전용 (표준 라이브러리만 사용).

`json.loads`가 조용히 받아들이는 입력을 모두 거부한다.
- 모든 수준의 중복 키 (마지막 값이 이기는 동작은 승인 상태 덮어쓰기에 악용될 수 있다)
- NaN / Infinity / -Infinity, 그리고 `1e999`처럼 무한대가 되는 수, 지나치게 긴 정수
- UTF-8 BOM, 잘못된 UTF-8, 짝이 없는 서로게이트(`"\\ud800"`)
- 1 MiB 초과 입력, 중첩 깊이 32 초과 (파싱 전에 검사하므로 RecursionError가 나지 않는다)

오류(`StrictJsonError`)는 고정 코드만 담는다. 파일 경로, 입력 값, 키 이름, 파싱 위치를 메시지나
예외 체인(`__context__`/`__cause__`)에 남기지 않는다. 호출자는 `.code`로 분기한다.

기존 `ops/legacy_weeks.json` 로더(`ops_state.load_legacy_registry`)는 이 모듈을 쓰지 않는다(이번 단계 범위 밖).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

MAX_BYTES = 1024 * 1024
MAX_DEPTH = 32
MAX_INT_DIGITS = 64

CODES = (
    "bad_input", "read_failed", "too_large", "bom", "bad_encoding", "too_deep", "invalid_json",
    "duplicate_key", "non_standard_number", "number_out_of_range", "invalid_string", "not_object", "empty",
)

_SURROGATE_RE = re.compile("[\ud800-\udfff]")
_INF = float("inf")


class StrictJsonError(ValueError):
    """고정 코드(`CODES` 중 하나)만 담는 오류. 입력·경로·키는 담지 않는다."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------
# 파싱 훅
# ---------------------------------------------------------------------------


def _object_pairs(pairs):
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise StrictJsonError("duplicate_key")
        seen.add(key)
    return dict(pairs)


def _parse_constant(_name):
    raise StrictJsonError("non_standard_number")


def _parse_int(text):
    if len(text.lstrip("-")) > MAX_INT_DIGITS:
        raise StrictJsonError("number_out_of_range")
    return int(text)


def _parse_float(text):
    value = float(text)
    if value != value or value in (_INF, -_INF):
        raise StrictJsonError("non_standard_number")
    return value


def _check_depth(text: str, max_depth: int) -> None:
    """문자열 안의 괄호를 세지 않는 중첩 깊이 검사 (유효하지 않은 JSON에서도 안전하게 동작)."""
    depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "[" or ch == "{":
            depth += 1
            if depth > max_depth:
                raise StrictJsonError("too_deep")
        elif ch == "]" or ch == "}":
            depth = depth - 1 if depth > 0 else 0


def _check_strings(value) -> None:
    """짝 없는 서로게이트가 값이나 키에 있으면 거부한다 (반복문 사용: 재귀하지 않는다)."""
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            if _SURROGATE_RE.search(node):
                raise StrictJsonError("invalid_string")
        elif isinstance(node, dict):
            for key, item in node.items():
                if _SURROGATE_RE.search(key):
                    raise StrictJsonError("invalid_string")
                stack.append(item)
        elif isinstance(node, list):
            stack.extend(node)


def _parse(text: str, max_depth: int, require_object: bool):
    code = None
    value = None
    try:
        _check_depth(text, max_depth)
        value = json.loads(
            text,
            object_pairs_hook=_object_pairs,
            parse_constant=_parse_constant,
            parse_int=_parse_int,
            parse_float=_parse_float,
        )
        _check_strings(value)
    except StrictJsonError as e:
        code = e.code
    except RecursionError:
        code = "too_deep"
    except ValueError:
        code = "invalid_json"
    if code is None and require_object and not isinstance(value, dict):
        code = "not_object"
    if code is not None:
        # except 블록 밖에서 새로 발생시켜 원본 예외(입력 문서를 담고 있다)가 체인에 남지 않게 한다.
        raise StrictJsonError(code)
    return value


def _limits(max_bytes: int, max_depth: int) -> None:
    if not (isinstance(max_bytes, int) and 0 < max_bytes <= MAX_BYTES):
        raise ValueError("max_bytes must be between 1 and MAX_BYTES")
    if not (isinstance(max_depth, int) and 0 < max_depth <= MAX_DEPTH):
        raise ValueError("max_depth must be between 1 and MAX_DEPTH")


# ---------------------------------------------------------------------------
# 공개 함수
# ---------------------------------------------------------------------------


def loads_bytes(data, *, max_bytes: int = MAX_BYTES, max_depth: int = MAX_DEPTH, require_object: bool = True):
    """UTF-8 바이트를 엄격하게 파싱한다. BOM·잘못된 UTF-8·크기 초과는 파싱 전에 거부한다."""
    _limits(max_bytes, max_depth)
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise StrictJsonError("bad_input")
    raw = bytes(data)
    if len(raw) > max_bytes:
        raise StrictJsonError("too_large")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise StrictJsonError("bom")
    text = None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if text is None:
        raise StrictJsonError("bad_encoding")
    if not text.strip():
        raise StrictJsonError("empty")
    return _parse(text, max_depth, require_object)


def loads_text(text, *, max_bytes: int = MAX_BYTES, max_depth: int = MAX_DEPTH, require_object: bool = True):
    """이미 디코딩된 문자열을 엄격하게 파싱한다 (크기는 UTF-8 바이트 수 기준)."""
    _limits(max_bytes, max_depth)
    if not isinstance(text, str):
        raise StrictJsonError("bad_input")
    if text.startswith("\ufeff"):
        raise StrictJsonError("bom")
    if len(text.encode("utf-8", errors="surrogatepass")) > max_bytes:
        raise StrictJsonError("too_large")
    if not text.strip():
        raise StrictJsonError("empty")
    return _parse(text, max_depth, require_object)


def load_path(path, *, max_bytes: int = MAX_BYTES, max_depth: int = MAX_DEPTH, require_object: bool = True):
    """파일을 최대 `max_bytes + 1` 바이트만 읽어 엄격하게 파싱한다. 오류에 경로를 남기지 않는다."""
    _limits(max_bytes, max_depth)
    data = None
    try:
        with open(Path(path), "rb") as fh:
            data = fh.read(max_bytes + 1)
    except (OSError, TypeError, ValueError):
        pass
    if data is None:
        raise StrictJsonError("read_failed")
    return loads_bytes(data, max_bytes=max_bytes, max_depth=max_depth, require_object=require_object)

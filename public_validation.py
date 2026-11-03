"""공개 저장소에 올라갈 레지스트리·정책 필드의 유형별 검증 (표준 라이브러리만 사용, 네트워크 호출 없음).

기존 `ops_state.assert_public_safe`(텍스트 전체 스캔)는 약관·라이선스 URL처럼 정당한 URL이 있는 문서 전체에 적용하면
모두 거부한다. 이 모듈은 필드를 유형으로 선언하고 유형별 규칙으로 검증한다.

    id        소문자·숫자·밑줄 식별자 (^[a-z0-9_]{1,40}$)
    enum      허용 값 목록에 정확히 있는 문자열
    ref       증빙·PR·핸들 같은 불투명 참조 (비밀 형태 거부)
    date      YYYY-MM-DD (유효한 달력 날짜)
    hostname  소문자 ASCII LDH/punycode 공개 호스트 (IP·와일드카드·포트·localhost·내부 도메인 거부)
    hash      소문자 64자리 16진수 (SHA-256)
    url       https 공개 URL (userinfo·query·fragment·IP·비표준 포트 거부, 300자 이하, 경로 문자 제한)
    text      일반 텍스트: 기존 `assert_public_safe`를 값 단위로 적용 (URL·키·로컬 경로·토큰 거부)

공통 규칙
- 모든 문자열은 NFC로 정규화한 값에 대해 검증하고 정규화한 값을 반환한다.
- 제어 문자, 양방향 제어(U+202A-202E, U+2066-2069, U+200E/F, U+061C), 제로폭 문자(U+200B-200D, U+2060, U+FEFF)를
  포함한 모든 형식(Cf) 문자, 서로게이트, 사용자 정의 영역(Co), 미할당(Cn), 줄·문단 구분자, 비ASCII 공백을 거부한다.
  (미할당 판정은 파이썬의 유니코드 데이터베이스 버전에 따라 달라질 수 있다.)
- 범주(Cf/Zs 등)만으로는 걸러지지 않는 "보이지 않는 글자"를 명시 목록으로 거부한다: 한글 채움 문자(U+115F, U+1160, U+3164, U+FFA0),
  브라유 공백(U+2800), 크메르 내재 모음(U+17B4, U+17B5), 몽골 자유 변이 선택자(U+180B-180D, U+180F), 변이 선택자(U+FE00-FE0F,
  U+E0100-E01EF), 결합 문자 접합자(U+034F), 음악 기호 빈 음표 머리(U+1D159). 이 글자가 하나라도 있거나, 눈에 보이는 글자
  (문자·숫자·구두점·기호)가 하나도 없는 문자열(결합 부호만 있는 문자열 등)은 거부한다.
  주의: 이모지 표현 선택자(U+FE0F)도 거부되므로 하트 이모지(U+2764) 뒤에 U+FE0F가 붙은 형태는 거부되고 선택자 없는 이모지는 허용된다.
- URL 경로의 퍼센트 인코딩은 디코딩한 결과까지 검증한다: 연속된 `%XX` 바이트열은 엄격한 UTF-8이어야 하고(잘못된 UTF-8, 과대 인코딩,
  서로게이트, U+10FFFF 초과, 잘린 시퀀스 거부), 디코딩된 문자에도 위의 문자 규칙을 다시 적용하며, NFKC로 `/`, 역슬래시, `.`, `%`, `?`, `#`, `@`, `:`가 되는
  호환 문자(전각 슬래시·마침표 등)와 이중 인코딩(`%25`)을 거부한다.
- 모든 검증 함수는 환경변수를 읽지 않는다(결정적). 비밀 패턴 검사는 `mask_secrets(text, {})`로 환경 값 없이 수행한다.
  환경변수에 든 알려진 비밀 값이 문자열에 들어 있는지는 별도 함수 `contains_env_secret(text, environ)`로만 확인한다(검증기는 호출하지 않음).
- 오류(`ValidationError`)는 고정 코드와 유형, (스키마가 정한) 필드 이름만 담는다. 입력 값은 담지 않는다.
- `assert_public_safe`/`mask_secrets`는 호출 시점에 지연 임포트한다 (`ops_state`와의 순환 임포트 방지).
  이 모듈은 `ops_state`·`backends`를 수정하거나 import 시점에 불러오지 않는다.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

KINDS = ("id", "enum", "ref", "date", "hostname", "hash", "url", "text")

ID_MAX = 40
REF_MAX = 64
URL_MAX = 300
TEXT_MAX = 200
HOSTNAME_MAX = 253

_ID_RE = re.compile(r"[a-z0-9_]{1,%d}" % ID_MAX)
_REF_RE = re.compile(r"[A-Za-z0-9#][A-Za-z0-9._:#-]*")
_DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_TLD_ALPHA_RE = re.compile(r"[a-z]{2,63}")
_TLD_PUNY_RE = re.compile(r"xn--[a-z0-9-]{1,59}")
_PATH_ALLOWED_RE = re.compile(r"[A-Za-z0-9._~/%:+-]*")
_PCT_RE = re.compile(r"%([0-9A-Fa-f]{2})")
_PCT_RUN_RE = re.compile(r"(?:%[0-9A-Fa-f]{2})+")
_HEX_RE = re.compile(r"[0-9A-Fa-f]+")

# 범주(Cf/Zs 등)로는 걸러지지 않지만 화면에 아무것도 그리지 않거나 다른 글자로 오인되는 글자
_INVISIBLE_CODE_POINTS = frozenset((
    0x034F,                        # COMBINING GRAPHEME JOINER
    0x115F, 0x1160,                # HANGUL CHOSEONG / JUNGSEONG FILLER
    0x17B4, 0x17B5,                # KHMER VOWEL INHERENT AQ / AA
    0x180B, 0x180C, 0x180D, 0x180F,  # MONGOLIAN FREE VARIATION SELECTORS (U+180E 는 Cf 로 이미 거부)
    0x2800,                        # BRAILLE PATTERN BLANK
    0x3164,                        # HANGUL FILLER
    0xFFA0,                        # HALFWIDTH HANGUL FILLER
    0x1D159,                       # MUSICAL SYMBOL NULL NOTEHEAD
))
_INVISIBLE_RANGES = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF))  # VARIATION SELECTORS (+ SUPPLEMENT)
_VISIBLE_CATEGORY_PREFIXES = "LNPS"  # 문자·숫자·구두점·기호
_COMPAT_SEPARATOR_CHARS = frozenset("/\\.%?#@:")
_ALNUM_RE = re.compile(r"[A-Za-z0-9]+")
_CHUNK_SPLIT_RE = re.compile(r"[-_.~:+]")

# 내부망·예약 최상위 도메인 (공개 호스트가 될 수 없다)
_RESERVED_TLDS = frozenset((
    "local", "localhost", "localdomain", "internal", "intranet", "lan", "home", "corp", "private",
    "test", "invalid", "example", "onion", "arpa", "workgroup",
))

_OPTIONS = {
    "id": {"max_length"},
    "enum": {"allowed"},
    "ref": {"max_length"},
    "date": set(),
    "hostname": set(),
    "hash": set(),
    "url": {"max_length"},
    "text": {"max_length", "allow_empty"},
}


class ValidationError(ValueError):
    """고정 코드만 담는 검증 오류. 입력 값은 담지 않는다."""

    def __init__(self, code: str, kind: str = "", field_name: str = ""):
        self.code = code
        self.kind = kind
        self.field = field_name
        label = ":".join(p for p in (field_name, kind, code) if p)
        super().__init__(label)


def _fail(kind: str, code: str):
    raise ValidationError(code, kind)


# ---------------------------------------------------------------------------
# 지연 임포트 (순환 임포트 방지)
# ---------------------------------------------------------------------------


def _mask_changes(text: str) -> bool:
    """비밀 **패턴**(키·토큰 형태, 로컬 사용자 경로 등)이 들어 있는지. 환경변수 값은 보지 않는다(결정적)."""
    from backends import mask_secrets  # 지연 임포트

    return mask_secrets(text, {}) != text


def contains_env_secret(text, environ) -> bool:
    """`environ`에 든 알려진 비밀 값(예: ANTHROPIC_API_KEY)이 `text`에 그대로 들어 있는지.

    환경에 따라 결과가 달라지는 검사이므로 일반 필드 검증기와 분리했다: 검증기(`validate_*`)는 이 함수를 호출하지 않고,
    호출자가 `os.environ` 같은 매핑을 **명시적으로** 넘길 때만 환경을 본다.
    """
    if not isinstance(text, str):
        raise ValidationError("not_a_string", "text")
    if not hasattr(environ, "get") or not hasattr(environ, "items"):
        raise TypeError("environ must be a mapping")
    from backends import mask_secrets  # 지연 임포트

    return mask_secrets(text, environ) != mask_secrets(text, {})


def _public_safe_rejects(text: str) -> bool:
    from ops_state import StatusError, assert_public_safe  # 지연 임포트: ops_state 가 이 모듈을 쓰게 되어도 순환하지 않는다

    rejected = False
    try:
        assert_public_safe(text)
    except StatusError:
        rejected = True
    return rejected


# ---------------------------------------------------------------------------
# 공통 문자열 검사
# ---------------------------------------------------------------------------


def _is_invisible(code_point: int) -> bool:
    if code_point in _INVISIBLE_CODE_POINTS:
        return True
    return any(low <= code_point <= high for low, high in _INVISIBLE_RANGES)


def _base(value, kind: str, *, max_length: int, ascii_only: bool = False, allow_space: bool = True,
          allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        _fail(kind, "not_a_string")
    if len(value) > max_length * 4 + 16:  # 정규화 비용을 제한하기 위한 선검사
        _fail(kind, "too_long")
    s = unicodedata.normalize("NFC", value)
    if len(s) > max_length:
        _fail(kind, "too_long")
    if not s:
        if allow_empty:
            return s
        _fail(kind, "empty")
    for ch in s:
        code_point = ord(ch)
        if ch == " ":
            if not allow_space:
                _fail(kind, "space")
            continue
        if code_point < 0x20 or code_point == 0x7F:
            _fail(kind, "control_char")
        if ascii_only and code_point > 0x7E:
            _fail(kind, "non_ascii")
        category = unicodedata.category(ch)
        if category == "Cc":
            _fail(kind, "control_char")
        if category == "Cf":
            _fail(kind, "bidi_or_zero_width_char")
        if _is_invisible(code_point):
            _fail(kind, "invisible_char")
        if category[0] == "C":
            _fail(kind, "forbidden_char")
        if category in ("Zl", "Zp", "Zs"):
            _fail(kind, "forbidden_space")
    if not any(unicodedata.category(ch)[0] in _VISIBLE_CATEGORY_PREFIXES for ch in s):
        _fail(kind, "no_visible_char")  # 결합 부호만 있는 문자열 등 화면에 아무것도 보이지 않는 값
    if s != s.strip(" "):
        _fail(kind, "edge_space")
    return s


def _char_problem(ch: str):
    """퍼센트 디코딩으로 얻은 문자에 적용하는 문자 규칙. 문제가 있으면 True."""
    code_point = ord(ch)
    if code_point < 0x20 or code_point == 0x7F:
        return True
    category = unicodedata.category(ch)
    if category[0] == "C" or category in ("Zl", "Zp", "Zs") or _is_invisible(code_point):
        return True
    return False


def _looks_like_token(chunk: str) -> bool:
    """고엔트로피 토큰처럼 보이는 영숫자 덩어리 (긴 영문 슬러그는 하이픈 등으로 나뉘어 통과한다)."""
    if len(chunk) >= 32 and _HEX_RE.fullmatch(chunk):
        return True
    if len(chunk) >= 24 and _ALNUM_RE.fullmatch(chunk):
        has_digit = any(c.isdigit() for c in chunk)
        has_alpha = any(c.isalpha() for c in chunk)
        if has_digit and has_alpha:
            return True
        upper = sum(1 for c in chunk if c.isupper())
        lower = sum(1 for c in chunk if c.islower())
        if len(chunk) >= 32 and upper >= 3 and lower >= 3:
            return True
    return False


# ---------------------------------------------------------------------------
# 유형별 검증
# ---------------------------------------------------------------------------


def validate_id(value, *, max_length: int = ID_MAX) -> str:
    s = _base(value, "id", max_length=max_length, ascii_only=True, allow_space=False)
    if not _ID_RE.fullmatch(s):
        _fail("id", "format")
    return s


def validate_enum(value, allowed) -> str:
    if not isinstance(allowed, (tuple, list, frozenset, set)) or not allowed \
            or not all(isinstance(a, str) and a for a in allowed):
        _fail("enum", "bad_allowed")
    if not isinstance(value, str):
        _fail("enum", "not_a_string")
    s = unicodedata.normalize("NFC", value) if len(value) <= 256 else _fail("enum", "too_long")
    if s not in allowed:
        _fail("enum", "not_allowed")
    return s


def validate_ref(value, *, max_length: int = REF_MAX) -> str:
    s = _base(value, "ref", max_length=max_length, ascii_only=True, allow_space=False)
    if not _REF_RE.fullmatch(s):
        _fail("ref", "format")
    if _looks_like_token(s) or any(_looks_like_token(c) for c in _CHUNK_SPLIT_RE.split(s)):
        _fail("ref", "secret_like")
    if _mask_changes(s):
        _fail("ref", "secret_like")
    return s


def validate_date(value) -> str:
    s = _base(value, "date", max_length=10, ascii_only=True, allow_space=False)
    m = _DATE_RE.fullmatch(s)
    if not m:
        _fail("date", "format")
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not 2000 <= year <= 2199:
        _fail("date", "range")
    valid = True
    try:
        date(year, month, day)
    except ValueError:
        valid = False
    if not valid:
        _fail("date", "calendar")
    return s


def validate_hash(value) -> str:
    s = _base(value, "hash", max_length=64, ascii_only=True, allow_space=False)
    if not _HASH_RE.fullmatch(s):
        _fail("hash", "format")
    return s


def _check_hostname(s: str, kind: str) -> None:
    if s != s.lower():
        _fail(kind, "uppercase")
    if s.endswith("."):
        _fail(kind, "trailing_dot")
    if "*" in s:
        _fail(kind, "wildcard")
    if ":" in s or "[" in s or "]" in s:
        _fail(kind, "port_or_ip_literal")
    labels = s.split(".")
    if len(labels) < 2:
        _fail(kind, "single_label")
    for label in labels:
        if not _LABEL_RE.fullmatch(label):
            _fail(kind, "label")
    tld = labels[-1]
    if not (_TLD_ALPHA_RE.fullmatch(tld) or _TLD_PUNY_RE.fullmatch(tld)):
        _fail(kind, "tld")  # 숫자/16진 형태의 마지막 레이블(127.1, 0x7f.0x1 등 IPv4 표기)을 모두 거부한다
    if tld in _RESERVED_TLDS:
        _fail(kind, "internal_domain")
    for label in labels:
        if label.startswith("xn--"):
            decoded = None
            try:
                decoded = label.encode("ascii").decode("idna")
            except UnicodeError:
                pass
            if decoded is None or not decoded or unicodedata.normalize("NFC", decoded) != decoded:
                _fail(kind, "punycode")
            for ch in decoded:
                if _char_problem(ch):
                    _fail(kind, "punycode")
            if not any(unicodedata.category(ch)[0] in _VISIBLE_CATEGORY_PREFIXES for ch in decoded):
                _fail(kind, "punycode")


def validate_hostname(value) -> str:
    s = _base(value, "hostname", max_length=HOSTNAME_MAX, ascii_only=True, allow_space=False)
    _check_hostname(s, "hostname")
    return s


def _pct_decode_ascii(segment: str) -> str:
    return _PCT_RE.sub(lambda m: chr(int(m.group(1), 16)) if int(m.group(1), 16) < 0x80 else m.group(0), segment)


def _check_percent_escapes(path: str) -> None:
    """경로의 모든 `%XX`를 검증한다 (바이트 단위 ASCII 규칙 + 연속된 상위 바이트의 엄격한 UTF-8 디코딩과 문자 규칙)."""
    for m in re.finditer("%", path):
        if not _PCT_RE.match(path, m.start()):
            _fail("url", "bad_percent")
    for run in _PCT_RUN_RE.finditer(path):
        data = bytes(int(h, 16) for h in _PCT_RE.findall(run.group()))
        i = 0
        while i < len(data):
            byte = data[i]
            if byte < 0x80:
                if byte == 0x25:
                    _fail("url", "double_encoding")  # %25 뒤에 16진수가 오는 이중 인코딩 우회를 포함해 %25 자체를 거부한다
                if byte < 0x20 or byte == 0x7F:
                    _fail("url", "control_escape")
                if byte in (0x2F, 0x5C):
                    _fail("url", "encoded_separator")
                i += 1
                continue
            j = i
            while j < len(data) and data[j] >= 0x80:
                j += 1
            decoded = None
            try:
                decoded = data[i:j].decode("utf-8")  # 엄격: 과대 인코딩·서로게이트·U+10FFFF 초과·잘린 시퀀스는 오류
            except UnicodeDecodeError:
                pass
            if decoded is None:
                _fail("url", "invalid_utf8")
            for ch in decoded:
                if _char_problem(ch):
                    _fail("url", "encoded_forbidden_char")
                if _COMPAT_SEPARATOR_CHARS & set(unicodedata.normalize("NFKC", ch)):
                    _fail("url", "compat_separator")
            i = j


def validate_url(value, *, max_length: int = URL_MAX) -> str:
    s = _base(value, "url", max_length=max_length, ascii_only=True, allow_space=False)
    if "\\" in s:
        _fail("url", "backslash")
    if not s.startswith("https://"):
        _fail("url", "scheme")
    if "?" in s:
        _fail("url", "query")
    if "#" in s:
        _fail("url", "fragment")
    rest = s[len("https://"):]
    netloc, slash, path_tail = rest.partition("/")
    path = slash + path_tail
    if not netloc:
        _fail("url", "host")
    if "@" in netloc:
        _fail("url", "userinfo")
    host = netloc
    if ":" in netloc:
        if netloc.count(":") != 1 or "[" in netloc:
            _fail("url", "ip_literal")
        host, _, port = netloc.partition(":")
        if port != "443":
            _fail("url", "port")
    _check_hostname(host, "url")
    if not _PATH_ALLOWED_RE.fullmatch(path):
        _fail("url", "path_chars")
    _check_percent_escapes(path)
    for segment in path.split("/"):
        decoded = _pct_decode_ascii(segment)
        if decoded in (".", ".."):
            _fail("url", "dot_segment")
        if _looks_like_token(decoded) or any(_looks_like_token(c) for c in _CHUNK_SPLIT_RE.split(decoded)):
            _fail("url", "secret_like")
    if _mask_changes(s):
        _fail("url", "secret_like")
    return s


def validate_text(value, *, max_length: int = TEXT_MAX, allow_empty: bool = False) -> str:
    s = _base(value, "text", max_length=max_length, allow_empty=allow_empty)
    if s and _public_safe_rejects(s):
        _fail("text", "unsafe_content")
    return s


_VALIDATORS = {
    "id": validate_id,
    "enum": validate_enum,
    "ref": validate_ref,
    "date": validate_date,
    "hostname": validate_hostname,
    "hash": validate_hash,
    "url": validate_url,
    "text": validate_text,
}


def validate_field(kind: str, value, **options):
    """유형 이름으로 검증한다. 알 수 없는 유형·옵션은 거부한다."""
    if kind not in _VALIDATORS:
        raise ValidationError("unknown_kind")
    if set(options) - _OPTIONS[kind]:
        raise ValidationError("bad_option", kind)
    return _VALIDATORS[kind](value, **options)


# ---------------------------------------------------------------------------
# 레코드(평탄한 문자열 필드 묶음) 검증
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    kind: str
    required: bool = True
    options: dict = field(default_factory=dict)


def check_schema(schema) -> None:
    if not isinstance(schema, dict) or not schema:
        raise ValidationError("bad_schema")
    for name, spec in schema.items():
        if not isinstance(name, str) or not _ID_RE.fullmatch(name) or not isinstance(spec, FieldSpec):
            raise ValidationError("bad_schema")
        if spec.kind not in _VALIDATORS:
            raise ValidationError("unknown_kind", field_name=name)
        if set(spec.options) - _OPTIONS[spec.kind]:
            raise ValidationError("bad_option", spec.kind, name)
        if spec.kind == "enum" and "allowed" not in spec.options:
            raise ValidationError("bad_option", spec.kind, name)


def validate_record(record, schema) -> dict:
    """스키마(필드 이름 -> FieldSpec)에 맞는지 검증하고 NFC 정규화한 새 dict를 반환한다.

    알 수 없는 필드, 누락된 필수 필드, 문자열이 아닌 값은 거부한다. 거부 오류에는 입력의 키·값을 담지 않는다.
    """
    check_schema(schema)
    if not isinstance(record, dict):
        raise ValidationError("not_object")
    for key in record:
        if not isinstance(key, str) or not _ID_RE.fullmatch(key):
            raise ValidationError("bad_key")
        if key not in schema:
            raise ValidationError("unknown_field")
    out: dict = {}
    for name, spec in schema.items():
        if name not in record:
            if spec.required:
                raise ValidationError("missing_field", field_name=name)
            continue
        error = None
        try:
            out[name] = _VALIDATORS[spec.kind](record[name], **spec.options)
        except ValidationError as e:
            error = ValidationError(e.code, e.kind, name)
        if error is not None:
            raise error
    return out
